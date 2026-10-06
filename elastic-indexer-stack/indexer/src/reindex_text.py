"""Resume extraction/rechunking of existing text records without replacing metadata."""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path

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


def reindex(client, index, repo, handler, embedder, record_ids=None, limit=0, batch_size=100):
    progress = Progress()
    after = None
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
    while not limit or progress.processed < limit:
        request = dict(index=index, query=query, source=FIELDS, size=min(batch_size, limit - progress.processed) if limit else batch_size,
                       sort=[{"id": "asc"}], seq_no_primary_term=True)
        if after is not None:
            request["search_after"] = after
        response = client.search(**request)
        if response.get("timed_out") or response.get("_shards", {}).get("failed"):
            raise ValueError("incomplete Elasticsearch scan")
        hits = response["hits"]["hits"]
        if not hits:
            break
        for hit in hits:
            try:
                reindex_record(client, index, hit, repo, handler, embedder, progress)
                upstream_failures = 0
            except ConflictError:
                progress.failed += 1
                LOGGER.warning("Concurrent update, left untouched: %s", hit["_id"])
            except Exception as error:
                progress.failed += 1
                # Log the type and public ID, not upstream payloads or secrets.
                LOGGER.error("Reindex failed (%s): %s", type(error).__name__, hit["_id"])
                if isinstance(error, (OpenAIError, TransportError)):
                    upstream_failures += 1
                    if upstream_failures >= 5:
                        raise RuntimeError("Repeated upstream failures; retry resumes completed versions") from None
            progress.processed += 1
        after = hits[-1]["sort"]
        LOGGER.info("Progress %s", asdict(progress))
    LOGGER.info("Finished %s", asdict(progress))
    return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(os.environ.get("LOCAL_REPO_PATH", "/data/eq-archives")))
    parser.add_argument("--id", action="append", dest="record_ids", help="probe exact IDs before the broad run")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 0 or not 1 <= args.batch_size <= 1000:
        parser.error("limit must be nonnegative and batch size must be 1–1000")
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
    progress = reindex(client, manager._index_name, args.repo, handler, OpenAIManager(),
                       args.record_ids, args.limit, args.batch_size)
    return 1 if progress.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
