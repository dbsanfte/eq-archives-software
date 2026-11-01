"""Checkpointed in-place EmbeddingGemma migration. Never calls paid enrichment."""
import argparse
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from dataclasses import asdict, dataclass
import logging
import multiprocessing
import os
from pathlib import Path
import threading

from elastic_transport import TransportError
from elasticsearch import ConflictError
from openai import OpenAIError
from langchain_core.documents import Document

from indexer import gemma
from indexer.es_manager import ElasticsearchManager
from indexer.html_extraction import extraction_version
from indexer.openai_manager import OpenAIManager
from indexer.reindex_preparation import handler, verify_prepared
from reindex_text import completed_records, source_path

LOG = logging.getLogger(__name__)
FIELDS = ["id", "file_type", "text_full", "text.text_chunk", "text_extraction_version",
          "embedding_model", "llm_summary", "llm_image_text_full", "llm_image_text"]


@dataclass
class Progress:
    processed: int = 0
    updated: int = 0
    unchanged: int = 0
    unavailable_sources: int = 0
    failed: int = 0


def remaining_query(ids=None):
    query = {"bool": {"filter": [{"exists": {"field": "id"}}],
                      "must_not": [{"term": {"embedding_model": gemma.MODEL}}],
                      "should": [{"term": {"file_type": "text"}}] + [
                          {"exists": {"field": name}} for name in
                          ("text_full", "llm_summary", "llm_image_text_full", "llm_image_text",
                           "llm_summary_vector", "llm_image_text_vector")],
                      "minimum_should_match": 1}}
    query["bool"]["should"].append({"nested": {"path": "text", "query": {"exists": {"field": "text.vector"}}}})
    if ids:
        query["bool"]["filter"].append({"ids": {"values": ids}})
    return query


def prepare(source, repo):
    """Read an old source only when extraction still needs repairing."""
    text = source.get("text_full") or ""
    patch = {}
    missing = 0
    record_id = source["id"]
    if source.get("file_type") == "text" and source.get("text_extraction_version") != extraction_version(record_id):
        path = source_path(Path(repo), record_id)
        if path.is_file():
            documents = handler()._get_documents_from_file(record_id, str(path))
            if len(documents) != 1 or text.strip() and not documents[0].page_content.strip():
                raise ValueError("Refusing incomplete or empty source replacement")
            text = documents[0].page_content
            patch.update(text_full=text, text_extraction_version=extraction_version(record_id))
        else:
            missing = 1
    # Legacy records lacking full text can still retain and reembed their exact
    # stored chunks. Do not invent repaired extraction or erase their content.
    texts = [text] if text.strip() else [row["text_chunk"] for row in source.get("text") or []
                                       if row.get("text_chunk", "").strip()]
    groups = [(body, gemma.chunk_source(body)) for body in texts]
    for body, chunks in groups:
        verify_prepared(body, chunks)
    return groups, patch, missing


def migrate_record(client, index, hit, repo, embedder, preparation_pool=None):
    source = hit["_source"]
    if source.get("id") != hit["_id"]:
        raise ValueError("Stored ID differs from exact Elasticsearch ID")
    if source.get("embedding_model") == gemma.MODEL:
        return 0, 0
    groups, patch, missing = (preparation_pool.submit(prepare, source, str(repo)).result()
                              if preparation_pool else prepare(source, str(repo)))
    chunks = []
    for body, prepared in groups:
        generated = embedder.get_chunks_and_embeddings(Document(page_content=body), _prepared_source=prepared)
        if len(generated) != len(prepared) or any(row["text_chunk"] != chunk.text for row, chunk in zip(generated, prepared)):
            raise ValueError("Embedding response omitted or changed source chunks")
        chunks.extend(generated)
    for row in chunks:
        gemma.vector(list(map(float, row["vector"])))
    # Every searched field is replaced in the SAME optimistic update. Empty
    # corresponding text removes an unusable old vector, preserving all text
    # and enrichment/provenance fields. No partially migrated document is ready.
    patch.update(text=chunks, text_chunking_version=gemma.CHUNKING_VERSION)
    for field, body in (("llm_summary_vector", source.get("llm_summary")),
                        ("llm_image_text_vector", source.get("llm_image_text_full") or source.get("llm_image_text"))):
        if isinstance(body, list):
            body = " ".join(body)
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
    return 1, missing


def reindex(client, index, repo, *, ids=None, limit=0, workers=4, batch_size=100,
            preparation_pool=None, factory=OpenAIManager):
    if not 1 <= workers <= 8 or limit < 0 or not 1 <= batch_size <= 1000:
        raise ValueError("Invalid migration bounds")
    query = remaining_query(ids)
    LOG.info("Starting Gemma migration: model=%s remaining=%s workers=%s", gemma.MODEL,
             client.count(index=index, query=query)["count"], workers)
    progress = Progress()
    local = threading.local()
    def execute(hit):
        if not hasattr(local, "embedder"):
            local.embedder = factory(embedding_model_name=gemma.MODEL)
        return migrate_record(client, index, hit, repo, local.embedder, preparation_pool)
    def scan():
        after = None
        scanned = 0
        while not limit or scanned < limit:
            request = dict(index=index, query=query, source=FIELDS, seq_no_primary_term=True,
                           sort=[{"id": "asc"}], size=min(batch_size, limit-scanned) if limit else batch_size)
            if after:
                request["search_after"] = after
            response = client.search(**request)
            if response.get("timed_out") or response.get("_shards", {}).get("failed"):
                raise ValueError("Incomplete Elasticsearch scan")
            hits = response["hits"]["hits"]
            if not hits:
                return
            for hit in hits:
                scanned += 1
                yield hit
            after = hits[-1]["sort"]
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for hit, result, _ in completed_records(scan(), execute, pool, workers):
            try:
                updated, missing = result()
                progress.updated += updated
                progress.unchanged += not updated
                progress.unavailable_sources += missing
                failures = 0
            except ConflictError:
                progress.failed += 1
                LOG.warning("Concurrent update retained: %s", hit["_id"])
            except Exception as error:
                progress.failed += 1
                LOG.error("Migration failed (%s): %s", type(error).__name__, hit["_id"])
                if isinstance(error, (OpenAIError, TransportError)):
                    failures += 1
                    if failures >= 5:
                        raise RuntimeError("Repeated upstream failures; retry resumes completed versions") from None
            progress.processed += 1
            if progress.processed % batch_size == 0:
                LOG.info("Progress %s", asdict(progress))
    LOG.info("Finished %s", asdict(progress))
    return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(os.getenv("LOCAL_REPO_PATH", "/data/eq-archives")))
    parser.add_argument("--id", action="append", dest="ids")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=int(os.getenv("REINDEX_WORKERS", "4")))
    parser.add_argument("--preparation-workers", type=int, default=int(os.getenv("REINDEX_PREPARATION_WORKERS", "2")))
    args = parser.parse_args()
    if not 0 <= args.preparation_workers <= 4:
        parser.error("Preparation workers must be 0–4")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("elastic_transport").setLevel(logging.WARNING)
    manager = ElasticsearchManager(manage_index=False)
    client = manager.get_client()
    client.indices.put_mapping(index=manager._index_name, properties={"embedding_model": {"type": "keyword"}})
    with ProcessPoolExecutor(max_workers=max(1, args.preparation_workers),
                             mp_context=multiprocessing.get_context("spawn")) as preparation_pool:
        progress = reindex(client, manager._index_name, args.repo, ids=args.ids, limit=args.limit,
                           workers=args.workers, preparation_pool=preparation_pool if args.preparation_workers else None)
    return 1 if progress.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
