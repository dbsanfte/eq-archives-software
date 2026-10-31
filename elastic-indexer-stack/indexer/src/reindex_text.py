"""Resume extraction/rechunking of existing text records without replacing metadata."""

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import threading

from elasticsearch import ConflictError
from elastic_transport import TransportError
from openai import OpenAIError
from langchain_core.documents import Document

from indexer.archive_handler import ArchiveHandler
from indexer.chunking import CHUNKING_VERSION
from indexer.es_manager import ElasticsearchManager, ES_FIELDS
from indexer.html_extraction import TEXT_EXTRACTION_VERSION, WEBSITE_EXTRACTION_VERSION, extraction_version
from indexer.openai_manager import OpenAIManager
from indexer.text_handler import TextHandler


LOGGER = logging.getLogger(__name__)
FIELDS = ["id", "text_full", "text_extraction_version", "text_chunking_version"]


@dataclass
class Progress:
    processed: int = 0
    updated: int = 0
    unchanged: int = 0
    unavailable_sources: int = 0
    empty_text: int = 0
    failed: int = 0
    primary_updated: int = 0
    secondary_updated: int = 0


def source_path(repo, record_id):
    path = Path(record_id)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] not in ("websites", "newsgroups", "mailing-lists"):
        raise ValueError("record ID is not a supported archive-relative source")
    resolved = (repo / path).resolve()
    if not resolved.is_relative_to(repo.resolve()):
        raise ValueError("source resolves outside the archive")
    return resolved


def reindex_record(client, index, hit, repo, handler, embedder, progress):
    record_id = hit["_id"]
    source = hit["_source"]
    if source.get("id") != record_id:
        raise ValueError("stored ID differs from exact Elasticsearch ID")
    path = source_path(repo, record_id)
    original = source.get("text_full") or ""
    text = original
    available = path.is_file()
    if available:
        documents = handler._get_documents_from_file(record_id, str(path))
        if len(documents) != 1:
            raise ValueError("expected one complete source document")
        text = documents[0].page_content
        if original.strip() and not text.strip():
            raise ValueError("refusing to replace existing text with empty extraction")
    else:
        # A sparse/older checkout must not delete preserved text or pretend it
        # has repaired extraction. Its existing full text can still be rechunked.
        progress.unavailable_sources += 1
    if not text.strip():
        # Legacy records can have chunks but no full text. Never erase those
        # vectors merely because complete source is unavailable.
        progress.empty_text += 1
        return
    patch = {}
    if text != original or source.get("text_chunking_version") != CHUNKING_VERSION:
        chunks = embedder.get_chunks_and_embeddings(Document(page_content=text)) if text.strip() else []
        if text.strip() and not chunks:
            raise ValueError("nonempty source has no embedding chunks")
        patch.update(text_full=text, text=chunks, text_chunking_version=CHUNKING_VERSION)
    version = extraction_version(record_id)
    if available and source.get("text_extraction_version") != version:
        patch["text_extraction_version"] = version
    if not patch:
        progress.unchanged += 1
        return
    patch["last_indexed"] = datetime.now(timezone.utc).isoformat()
    # An atomic partial update retains enrichment, OCR, provenance and all
    # unknown fields. A concurrent writer cannot be overwritten by this scan.
    response = client.update(index=index, id=record_id, doc=patch,
                             if_seq_no=hit["_seq_no"], if_primary_term=hit["_primary_term"])
    if response.get("result") not in ("updated", "noop"):
        raise ValueError("Elasticsearch did not update the record")
    progress.updated += 1


def completed_records(hits, execute, pool, workers):
    """Keep at most workers records in flight, including on an upstream failure."""
    if pool is None:
        for hit in hits:
            yield hit, execute, None
        return
    remaining = iter(hits)
    pending = {}
    # Consume lazily: neither a large ES page nor one slow document may create
    # an unbounded backlog or prevent the other slots doing useful work.
    for _ in range(workers):
        hit = next(remaining, None)
        if hit is not None:
            pending[pool.submit(execute, hit)] = hit
    try:
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                hit = pending.pop(future)
                yield hit, future.result, True
                hit = next(remaining, None)
                if hit is not None:
                    pending[pool.submit(execute, hit)] = hit
    finally:
        for future in pending:
            future.cancel()


def reindex(client, index, repo, handler, embedder, record_ids=None, limit=0, batch_size=100,
            *, workers=1, worker_factory=None):
    if type(workers) is not int or not 1 <= workers <= 8 or workers > 1 and worker_factory is None:
        raise ValueError("Use 1–8 workers; concurrent workers require isolated handlers/clients")
    progress = Progress()
    local = threading.local()
    def execute(hit):
        if not hasattr(local, 'resources'):
            local.resources = worker_factory() if worker_factory else (handler, embedder)
        delta = Progress()
        try:
            reindex_record(client, index, hit, repo, *local.resources, delta)
        except Exception as error:
            return delta, error
        if getattr(local.resources[1], '_reindex_backend', None) == 'secondary':
            delta.secondary_updated = delta.updated
        else:
            delta.primary_updated = delta.updated
        return delta, None

    with ThreadPoolExecutor(max_workers=workers) if workers > 1 else nullcontext() as pool:
        return _reindex(client, index, record_ids, limit, batch_size, progress, execute, pool, workers)


def _reindex(client, index, record_ids, limit, batch_size, progress, execute, pool, workers):
    upstream_failures = 0
    # Stable archive IDs allow live search_after paging without holding old ES
    # segments for a multi-day scroll/PIT while this job rewrites the index.
    query = {"bool": {
        "filter": [{"term": {"file_type": "text"}}, {"exists": {"field": "id"}}],
        "must_not": [{"bool": {
            "filter": [{"term": {"text_chunking_version": CHUNKING_VERSION}}],
            "should": [
                {"bool": {"filter": [
                    {"prefix": {"id": "websites/"}},
                    {"term": {"text_extraction_version": WEBSITE_EXTRACTION_VERSION}}
                ]}},
                {"bool": {
                    "filter": [{"term": {"text_extraction_version": TEXT_EXTRACTION_VERSION}}],
                    "must_not": [{"prefix": {"id": "websites/"}}]
                }}
            ],
            "minimum_should_match": 1
        }}]
    }}
    if record_ids:
        query["bool"]["filter"].append({"ids": {"values": record_ids}})
    total = client.count(index=index, query=query)["count"]
    LOGGER.info("Starting %s candidates (live index); website_extraction=%s other_extraction=%s chunking=%s",
                total, WEBSITE_EXTRACTION_VERSION, TEXT_EXTRACTION_VERSION, CHUNKING_VERSION)
    def scan():
        after = None
        scanned = 0
        while not limit or scanned < limit:
            request = dict(index=index, query=query, source=FIELDS,
                           size=min(batch_size, limit - scanned) if limit else batch_size,
                           sort=[{"id": "asc"}], seq_no_primary_term=True)
            if after is not None:
                request["search_after"] = after
            response = client.search(**request)
            if response.get("timed_out") or response.get("_shards", {}).get("failed"):
                raise ValueError("incomplete Elasticsearch scan")
            hits = response["hits"]["hits"]
            if not hits:
                return
            for hit in hits:
                scanned += 1
                yield hit
            after = hits[-1]["sort"]

    # Keep fast workers fed across search pages: a slower secondary GPU must
    # never introduce a barrier at every page boundary.
    for hit, result, asynchronous in completed_records(scan(), execute, pool, workers):
        try:
            delta, error = result() if asynchronous else result(hit)
            for field, value in asdict(delta).items():
                setattr(progress, field, getattr(progress, field) + value)
            if error is not None:
                raise error
            upstream_failures = 0
        except ConflictError:
            progress.failed += 1
            LOGGER.warning("Concurrent update, left untouched: %s", hit["_id"])
        except Exception as error:
            progress.failed += 1
            LOGGER.error("Reindex failed (%s): %s", type(error).__name__, hit["_id"])
            if isinstance(error, (OpenAIError, TransportError)):
                upstream_failures += 1
                if upstream_failures >= 5:
                    raise RuntimeError("Repeated upstream failures; retry resumes completed versions") from None
        progress.processed += 1
        if progress.processed % batch_size == 0:
            LOGGER.info("Progress %s", asdict(progress))
    LOGGER.info("Finished %s", asdict(progress))
    return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(os.environ.get("LOCAL_REPO_PATH", "/data/eq-archives")))
    parser.add_argument("--id", action="append", dest="record_ids", help="probe exact IDs before the broad run")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--workers", type=int, default=int(os.environ.get("REINDEX_WORKERS", "1")))
    parser.add_argument("--secondary-endpoint", default=os.environ.get("REINDEX_SECONDARY_ENDPOINT"))
    parser.add_argument("--secondary-workers", type=int, default=int(os.environ.get("REINDEX_SECONDARY_WORKERS", "1")))
    args = parser.parse_args()
    if args.limit < 0 or not 1 <= args.batch_size <= 1000 or not 1 <= args.workers <= 8:
        parser.error("limit must be nonnegative, batch size 1–1000 and workers 1–8")
    total_workers = args.workers + (args.secondary_workers if args.secondary_endpoint else 0)
    if not 1 <= args.secondary_workers <= 2 or total_workers > 8:
        parser.error("secondary workers must be 1–2 and total workers at most 8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("elastic_transport").setLevel(logging.WARNING)
    # This job never creates/reinitializes the index or runs LLM enrichment.
    manager = ElasticsearchManager(manage_index=False)
    client = manager.get_client()
    client.indices.put_mapping(index=manager._index_name, properties={
        name: ES_FIELDS[name]["mapping"] for name in ("text_extraction_version", "text_chunking_version")
    })
    handler = TextHandler(ArchiveHandler(), None, llm_enrichment_enabled=False)
    factory_lock = threading.Lock()
    assigned = 0
    def worker_factory():
        nonlocal assigned
        with factory_lock:
            secondary = assigned >= args.workers
            assigned += 1
        worker_embedder = OpenAIManager(base_url_embeddings=args.secondary_endpoint) if secondary else OpenAIManager()
        if secondary:
            worker_embedder._nomic_profile = 'vulkan'
            worker_embedder._nomic_batch_size = '2'
            worker_embedder._nomic_encoding = 'float'
        worker_embedder._reindex_backend = 'secondary' if secondary else 'primary'
        return TextHandler(ArchiveHandler(), None, llm_enrichment_enabled=False), worker_embedder
    progress = reindex(client, manager._index_name, args.repo, handler, OpenAIManager(),
                       args.record_ids, args.limit, args.batch_size, workers=total_workers,
                       worker_factory=worker_factory if total_workers > 1 else None)
    return 1 if progress.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
