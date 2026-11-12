"""Checkpointed in-place EmbeddingGemma migration. Never calls paid enrichment."""
import argparse
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from dataclasses import asdict, dataclass
import logging
import multiprocessing
import os
from pathlib import Path
import re
import threading
import tempfile

from elastic_transport import TransportError
from elasticsearch import ConflictError
from openai import OpenAIError
from langchain_core.documents import Document
from bs4 import BeautifulSoup

from indexer import gemma
from indexer import passages
from indexer.es_manager import ElasticsearchManager
from indexer.html_extraction import extraction_version, WEBSITE_EXTRACTION_VERSION
from indexer.openai_manager import OpenAIManager
from indexer.reindex_diagnostics import Ledger,source_hash,existing_report
from indexer.reindex_preparation import handler, verify_prepared
from indexer.source_encoding import repaired_title
from indexer.source_files import Sources, SourceError
from reindex_text import completed_records, source_path

LOG = logging.getLogger(__name__)
FIELDS = ["id", "file_type", "text_full", "text.text_chunk", "text_extraction_version",
          "text.title", "text.context", "text.kind", "text.source_start", "text.source_end",
          "embedding_model", "text_chunking_version", "mime_type", "title",
          "llm_summary", "llm_image_text_full", "llm_image_text", "text_passage_provenance",
          "archive_source_sha256", "archive_commit"]


def website_text_query():
    return {'bool':{'filter':[{'prefix':{'id':'websites/'}},{'term':{'file_type':'text'}}],
                   'should':[{'bool':{'must_not':[{'exists':{'field':'mime_type'}}]}},
                             {'term':{'mime_type':''}},{'prefix':{'mime_type':'text/'}},
                             {'terms':{'mime_type':['application/xhtml+xml','application/xml','application/json']}}],
                   'minimum_should_match':1}}


def current_source(source):
    mime=source.get('mime_type') or ''
    website=(source['id'].startswith('websites/') and source.get('file_type')=='text'
             and (not mime or mime.startswith('text/') or mime in ('application/xhtml+xml','application/xml','application/json')))
    return not website or source.get('text_extraction_version')==WEBSITE_EXTRACTION_VERSION


@dataclass
class Progress:
    processed: int = 0
    updated: int = 0
    unchanged: int = 0
    unavailable_sources: int = 0
    failed: int = 0
    retryable_failed: int = 0
    passages: int = 0
    post_passages: int = 0
    table_passages: int = 0
    quote_passages: int = 0
    short_passages: int = 0
    excluded_documents: int = 0
    reused_embeddings: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_evictions: int = 0


def remaining_query(ids=None):
    query = {"bool": {"filter": [{"exists": {"field": "id"}}],
                      "must_not": [{"bool": {"filter": [
                          {"term": {"embedding_model": gemma.MODEL}},
                          {"term": {"text_chunking_version": passages.VERSION}},
                          {'bool':{'should':[{'bool':{'must_not':[website_text_query()]}},
                                             {'term':{'text_extraction_version':WEBSITE_EXTRACTION_VERSION}}],
                                   'minimum_should_match':1}}]}}],
                      "should": [{"term": {"file_type": "text"}}] + [
                          {"exists": {"field": name}} for name in
                          ("text_full", "llm_summary", "llm_image_text_full", "llm_image_text",
                           "llm_summary_vector", "llm_image_text_vector")],
                      "minimum_should_match": 1}}
    query["bool"]["should"].append({"nested": {"path": "text", "query": {"exists": {"field": "text.vector"}}}})
    if ids is not None:
        if not 1<=len(ids)<=1000 or any(not isinstance(value,str) or not value for value in ids):
            raise ValueError("Explicit retries require 1–1000 exact record IDs")
        query["bool"]["filter"].append({"ids": {"values": ids}})
    return query


def prepare(source, repo, publication_repo=None):
    """Resolve a missing source from verified local Git, never from a remote."""
    path=source_path(Path(repo),source['id'])
    if (not path.is_file() and source.get('file_type')=='text'
            and (publication_repo or (Path(repo)/'.git/objects').is_dir())):
        with Sources([repo],publication_repo) as sources:
            try:data=sources.read(source)
            except SourceError as error:
                if error.reason!='source_unavailable':raise
            else:
                # A single bounded file is temporary; the prepared checkout,
                # its index and source objects remain read-only and untouched.
                with tempfile.TemporaryDirectory(prefix='reindex-source-') as temporary:
                    target=Path(temporary)/source['id']
                    target.parent.mkdir(parents=True)
                    target.write_bytes(data)
                    return _prepare_local(source,Path(temporary))
    return _prepare_local(source,repo)


def _prepare_local(source, repo):
    """Read an old source only when extraction still needs repairing."""
    text = source.get("text_full") or ""
    patch = {}
    missing = 0
    record_id = source["id"]
    mime=source.get("mime_type") or ""
    textual=not mime or mime.startswith("text/") or mime in ("application/xhtml+xml","application/xml","application/json")
    decoded=None;reuse_reader=False
    previous_plan=source.get('text_passage_provenance') or {}
    previous=previous_plan.get('decoding') or {}
    bindings=previous_plan.get('sources',[previous_plan])
    bound_reader=(isinstance(bindings,list) and len(bindings)==1 and isinstance(bindings[0],dict)
                  and bindings[0].get('source_sha256')==passages.digest(text))
    if (record_id.startswith('websites/') and source.get('file_type')=='text' and textual and text.strip()
            and source.get('text_extraction_version')=='html-2026-11-05-regional-decoding'
            and previous.get('version') in ('source-decoding-v2','source-decoding-v3') and bound_reader
            and re.fullmatch('[0-9a-f]{64}',previous.get('source_sha256',''))):
        path=source_path(Path(repo),record_id)
        if path.is_file():
            decoded=handler()._read_decoded_file(str(path))
            reuse_reader=({k:v for k,v in previous.items() if k!='version'}==
                          {k:v for k,v in decoded.provenance().items() if k!='version'})
            if reuse_reader:patch['text_extraction_version']=WEBSITE_EXTRACTION_VERSION
    if source.get("file_type") == "text" and textual and source.get("text_extraction_version") != extraction_version(record_id) and not reuse_reader:
        path = source_path(Path(repo), record_id)
        if path.is_file():
            documents = handler()._get_documents_from_file(record_id, str(path))
            if len(documents) != 1 or text.strip() and not documents[0].page_content.strip():
                raise ValueError("Refusing incomplete or empty source replacement")
            text = documents[0].page_content
            patch['text_extraction_version']=extraction_version(record_id)
            if text!=source.get('text_full'):patch['text_full']=text
        else:
            missing = 1
    # Legacy records lacking full text can still retain and reembed their exact
    # stored chunks. Do not invent repaired extraction or erase their content.
    texts = [text] if text.strip() else [row["text_chunk"] for row in source.get("text") or []
                                       if row.get("text_chunk", "").strip()]
    raw = None
    decoding=None
    if record_id.startswith("websites/") and textual:
        path = source_path(Path(repo), record_id)
        if path.is_file():
            decoded=decoded or handler()._read_decoded_file(str(path))
            raw=decoded.text;decoding=decoded.provenance()
            title=BeautifulSoup(raw,'html.parser').title
            corrected=repaired_title(source.get('title'),title.string if title else None,decoded)
            if corrected!=source.get('title'):patch['title']=corrected
    groups = [(body, passages.prepare(body, record_id, patch.get("title",source.get("title")) or "",
               html_source=raw, mime_type=source.get("mime_type") or "")) for body in texts]
    # Error/supporting projections retain their existing reader. Bind the
    # empty semantic view to that retained reader too, rather than checkpointing
    # a different extraction and making the next policy reject its provenance.
    if ((source.get('text_full') or '').strip() and any(plan.method=='archive_status' or
            'supporting_asset' in plan.exclusions for _,plan in groups)):
        body=source['text_full'];patch.pop('text_full',None)
        groups=[(body,passages.prepare(body,record_id,patch.get('title',source.get('title')) or '',
                html_source=raw,mime_type=source.get('mime_type') or ''))]
    for body, plan in groups:
        passages.verify(body, plan)
    if decoding:patch['_source_decoding']=decoding
    return groups, patch, missing


def migrate_record(client, index, hit, repo, embedder, preparation_pool=None, *, diagnostics=None, publication_repo=None, source_proof=None):
    source = hit["_source"]
    if source.get("id") != hit["_id"]:
        raise ValueError("Stored ID differs from exact Elasticsearch ID")
    previous=re.fullmatch(r'eq-passages-480-v(\d+)',source.get('text_chunking_version') or '')
    current=re.fullmatch(r'eq-passages-480-v(\d+)',passages.VERSION)
    if (source.get('embedding_model')==gemma.MODEL and previous and current and int(previous[1])>int(current[1])):
        return 0,0  # An older continuation must never downgrade a newer writer.
    complete=source.get("embedding_model") == gemma.MODEL and source.get("text_chunking_version") == passages.VERSION
    if complete and current_source(source):
        return 0, 0
    arguments=(source,str(repo),str(publication_repo)) if publication_repo else (source,str(repo))
    groups, patch, missing = (preparation_pool.submit(prepare, *arguments).result()
                              if preparation_pool else prepare(*arguments))
    decoding=patch.pop('_source_decoding',None)
    provenance={'version':passages.VERSION,'sources':[plan.provenance() for _,plan in groups]}
    if decoding:provenance['decoding']=decoding
    previous=source.get('text_passage_provenance') or {}
    # A decoding checkpoint is not a new embedding model or passage policy.
    # Reuse vectors when the full reader and exact source-bound search plans
    # are unchanged; do not pay GPU work to mark clean historical sources.
    prior_version=source.get('text_chunking_version')
    raw_hash=(decoding or {}).get('source_sha256')
    same_raw=(isinstance(raw_hash,str) and re.fullmatch(r'[0-9a-f]{64}',raw_hash)
              and raw_hash==(previous.get('decoding') or {}).get('source_sha256'))
    # Imports preserve CRLF while the legacy reader normalizes it. A verified
    # identical byte source plus identical reader/view/request data is stronger
    # evidence than the intermediate decoded-HTML representation's hash.
    ignored={'version','html_sha256'} if same_raw else {'version'}
    same_sources=([{k:v for k,v in p.items() if k not in ignored} for p in previous.get('sources',[])]
                  ==[{k:v for k,v in p.items() if k not in ignored} for p in provenance['sources']])
    expected=[(p.text,p.title,p.context,p.kind,p.start,p.end) for _,plan in groups for p in plan.passages]
    saved=[tuple(p.get(k) for k in ('text_chunk','title','context','kind','source_start','source_end'))
           for p in source.get('text') or []]
    reuse=(source.get('embedding_model')==gemma.MODEL and not missing
           and prior_version in ('eq-passages-480-v4','eq-passages-480-v5','eq-passages-480-v6','eq-passages-480-v7','eq-passages-480-v8','eq-passages-480-v9','eq-passages-480-v10','eq-passages-480-v11',passages.VERSION) and previous.get('version')==prior_version
           and patch.get('text_full',source.get('text_full'))==source.get('text_full')
           and same_sources and expected==saved)
    if reuse:
        patch['text_passage_provenance']=provenance
        patch['text_chunking_version']=passages.VERSION
        response=client.update(index=index,id=hit['_id'],doc=patch,if_seq_no=hit['_seq_no'],if_primary_term=hit['_primary_term'])
        if response.get('result') not in ('updated','noop'):raise ValueError('Elasticsearch did not update the record')
        if diagnostics is not None:
            diagnostics['reused_embeddings']=1
        if source_proof is not None and raw_hash:source_proof['sha256']=raw_hash
        return 1,0
    chunks = []
    for body, prepared in groups:
        generated = embedder.get_chunks_and_embeddings(Document(page_content=body), _prepared_passages=prepared)
        if len(generated) != len(prepared.passages) or any(row["text_chunk"] != chunk.text for row, chunk in zip(generated, prepared.passages)):
            raise ValueError("Embedding response omitted or changed source chunks")
        chunks.extend(generated)
    for row in chunks:
        gemma.vector(list(map(float, row["vector"])))
    # Every searched field is replaced in the SAME optimistic update. Empty
    # corresponding text removes an unusable old vector, preserving all text
    # and enrichment/provenance fields. No partially migrated document is ready.
    patch.update(text=chunks, text_chunking_version=passages.VERSION,
                 text_passage_provenance=provenance)
    excluded = any(plan.method=="archive_status" or "supporting_asset" in plan.exclusions
                   for _, plan in groups)
    for field, body in (("llm_summary_vector", source.get("llm_summary")),
                        ("llm_image_text_vector", source.get("llm_image_text_full") or source.get("llm_image_text"))):
        body = None if excluded else passages.useful_summary(body)
        value = embedder.embed_text(body) if body and body.strip() else None
        if body and body.strip() and value is None:
            raise ValueError("Embedding response omitted summary/OCR vector")
        patch[field] = gemma.vector(list(map(float, value))) if value is not None else None
    patch["embedding_model"] = gemma.MODEL
    # Preserve last_indexed: this is a model migration, not a newly captured site.
    response = client.update(index=index, id=hit["_id"], doc=patch,
                             if_seq_no=hit["_seq_no"], if_primary_term=hit["_primary_term"])
    if response.get("result") not in ("updated", "noop"):
        raise ValueError("Elasticsearch did not update the record")
    if diagnostics is not None:
        selected=[p for _,plan in groups for p in plan.passages]
        diagnostics.update(passages=len(selected),post_passages=sum(p.kind=='post' for p in selected),
                           table_passages=sum(p.kind=='table' for p in selected),quote_passages=sum(p.kind=='quote' for p in selected),
                           short_passages=sum(len(p.text.strip())<80 for p in selected),excluded_documents=int(excluded))
    if source_proof is not None and raw_hash:source_proof['sha256']=raw_hash
    return 1, missing


def reindex(client, index, repo, *, ids=None, limit=0, workers=4, batch_size=100,
            preparation_pool=None, factory=OpenAIManager, prioritize_unconverted=False,ledger=None,include_completed=False,publication_repo=None):
    if not 1 <= workers <= 8 or limit < 0 or not 1 <= batch_size <= 1000:
        raise ValueError("Invalid migration bounds")
    query = remaining_query(ids)
    if include_completed:
        if not ids:raise ValueError('Completed-record retries require bounded explicit IDs')
        query['bool']['must_not']=[]
    LOG.info("Starting Gemma migration: model=%s policy=%s remaining=%s workers=%s", gemma.MODEL, passages.VERSION,
             client.count(index=index, query=query)["count"], workers)
    progress = Progress()
    local = threading.local()
    def execute(hit):
        if not hasattr(local, "embedder"):
            local.embedder = factory(embedding_model_name=gemma.MODEL)
        metrics=getattr(local.embedder,'embedding_cache_metrics',lambda:{})
        before=metrics();before=before if isinstance(before,dict) else {}
        diagnostics={};proof={}
        result=migrate_record(client, index, hit, repo, local.embedder, preparation_pool,diagnostics=diagnostics,publication_repo=publication_repo,source_proof=proof)
        after=metrics()
        if result[0] and isinstance(after,dict):
            diagnostics.update({'cache_'+key:after.get(key,0)-before.get(key,0) for key in ('hits','misses','evictions')})
        return *result,diagnostics,proof.get("sha256")
    def scan():
        scanned = 0
        queries=[query]
        if prioritize_unconverted:
            first=deepcopy(query);first['bool']['must_not'].append({'term':{'embedding_model':gemma.MODEL}})
            second=deepcopy(query);second['bool']['filter'].append({'term':{'embedding_model':gemma.MODEL}})
            queries=[first,second]
        for phase,selection in enumerate(queries,1):
            LOG.info('Starting passage scan phase %s/%s',phase,len(queries))
            after=None
            while not limit or scanned < limit:
                request = dict(index=index, query=selection, source=FIELDS, seq_no_primary_term=True,
                               sort=[{"id": "asc"}], size=min(batch_size, limit-scanned) if limit else batch_size)
                if after:
                    request["search_after"] = after
                response = client.search(**request)
                if response.get("timed_out") or response.get("_shards", {}).get("failed"):
                    raise ValueError("Incomplete Elasticsearch scan")
                hits = response["hits"]["hits"]
                if not hits:
                    break
                for hit in hits:
                    scanned += 1
                    yield hit
                after = hits[-1]["sort"]
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for hit, result, _ in completed_records(scan(), execute, pool, workers):
            try:
                updated, missing, diagnostics, verified_hash = result()
            except ConflictError as error:
                progress.failed += 1
                progress.retryable_failed += 1
                LOG.warning("Concurrent update retained: %s", hit["_id"])
                if ledger:ledger.record(hit['_id'],model=gemma.MODEL,policy=passages.VERSION,
                                        source_sha256=source_hash(hit['_source'],repo),error=error,job=os.environ.get('REINDEX_JOB_NAME',''))
            except Exception as error:
                progress.failed += 1
                LOG.error("Migration failed (%s): %s", type(error).__name__, hit["_id"])
                if ledger:ledger.record(hit['_id'],model=gemma.MODEL,policy=passages.VERSION,
                                        source_sha256=source_hash(hit['_source'],repo),error=error,job=os.environ.get('REINDEX_JOB_NAME',''))
                if isinstance(error, (OpenAIError, TransportError)):
                    progress.retryable_failed += 1
                    failures += 1
                    if failures >= 5:
                        raise RuntimeError("Repeated upstream failures; retry resumes completed versions") from None
            else:
                progress.updated += updated
                progress.unchanged += not updated
                progress.unavailable_sources += missing
                for key,value in diagnostics.items():setattr(progress,key,getattr(progress,key)+value)
                failures = 0
                if ledger:
                    if missing:ledger.record(hit['_id'],model=gemma.MODEL,policy=passages.VERSION,
                                             category='source_unavailable',job=os.environ.get('REINDEX_JOB_NAME',''))
                    elif (ledger.has_pending(hit['_id']) and (updated or
                            hit['_source'].get('text_chunking_version')==passages.VERSION and current_source(hit['_source']))):
                        ledger.resolve(hit['_id'],policy=passages.VERSION,source_sha256=verified_hash or source_hash(hit['_source'],repo) or
                                           (hit['_source'].get('text_passage_provenance') or {}).get('decoding',{}).get('source_sha256'),
                                       job=os.environ.get('REINDEX_JOB_NAME',''))
            progress.processed += 1
            if progress.processed % batch_size == 0:
                LOG.info("Progress %s", asdict(progress))
    LOG.info("Finished %s", asdict(progress))
    return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(os.getenv("LOCAL_REPO_PATH", "/data/eq-archives")))
    parser.add_argument("--id", action="append", dest="ids")
    parser.add_argument("--publication-repo",type=Path,default=os.getenv("REINDEX_PUBLICATION_REPO"),
                        help="Read-only local publication Git objects for capture source recovery")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=int(os.getenv("REINDEX_WORKERS", "4")))
    parser.add_argument("--preparation-workers", type=int, default=int(os.getenv("REINDEX_PREPARATION_WORKERS", "2")))
    parser.add_argument("--prioritize-unconverted",action="store_true",
                        default=os.getenv('REINDEX_PRIORITIZE_UNCONVERTED','false')=='true')
    parser.add_argument('--diagnostics',type=Path,default=os.environ.get('REINDEX_DIAGNOSTICS_PATH'))
    parser.add_argument('--retry-failed',action='store_true',help='Retry at most 1000 persistent pending exceptions')
    args = parser.parse_args()
    if not 0 <= args.preparation_workers <= 4:
        parser.error("Preparation workers must be 0–4")
    if args.retry_failed and not args.diagnostics:parser.error('--retry-failed requires persistent diagnostics')
    if args.retry_failed and args.ids:parser.error('Choose --retry-failed or explicit --id records')
    if args.retry_failed:
        try:existing_report(args.diagnostics,limit=1)
        except Exception as error:parser.error('Existing retry diagnostics are unavailable ('+type(error).__name__+')')
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("elastic_transport").setLevel(logging.WARNING)
    manager = ElasticsearchManager(manage_index=False)
    client = manager.get_client()
    client.indices.put_mapping(index=manager._index_name, properties={
        "embedding_model": {"type": "keyword"}, "text_chunking_version": {"type": "keyword"},
        "text_passage_provenance": {"type": "object", "enabled": False}})
    ledger=Ledger(args.diagnostics) if args.diagnostics else None
    try:
        ids=ledger.pending_ids() if args.retry_failed else args.ids
        if args.retry_failed and not ids:return 0
        with ProcessPoolExecutor(max_workers=max(1, args.preparation_workers),
                                 mp_context=multiprocessing.get_context("spawn")) as preparation_pool:
            progress = reindex(client, manager._index_name, args.repo, ids=ids, limit=args.limit,
                               workers=args.workers, preparation_pool=preparation_pool if args.preparation_workers else None,
                               prioritize_unconverted=args.prioritize_unconverted,ledger=ledger,include_completed=args.retry_failed,publication_repo=args.publication_repo)
    finally:
        if ledger:ledger.close()
    return (1 if progress.retryable_failed else 43) if progress.failed or progress.unavailable_sources else 0


if __name__ == "__main__":
    raise SystemExit(main())
