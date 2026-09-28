"""Index the verified BlueWorld alt.games.everquest recovery without RabbitMQ.

The archive checkout is mounted read-only. The job validates the entire source
set before writing and can resume after interruption by skipping existing IDs.
"""

import argparse
import csv
import hashlib
import logging
import os
import time
from pathlib import Path

from indexer.es_manager import ElasticsearchManager
from indexer.indexer import Indexer
from indexer.openai_manager import OpenAIManager


RELATIVE_DIR = Path("newsgroups/blueworld-pre2000")
EXPECTED_COUNT = 11914
EXPECTED_COLUMNS = ["article_number", "message_id", "date", "sha256", "bytes", "utf8_valid"]
LOGGER = logging.getLogger(__name__)


def split_text(text: str, max_chars: int = 1200) -> list[str]:
    """Make complete, bounded chunks; boundaries preserve every source character."""
    if max_chars < 2:
        raise ValueError("max_chars must be at least 2")
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(text.rfind(" ", start + max_chars // 2, end),
                           text.rfind("\n", start + max_chars // 2, end))
            if boundary >= 0:
                end = boundary + 1
        chunks.append(text[start:end])
        start = end
    return chunks


class BoundedArchiveEmbedder(OpenAIManager):
    """Use the deployed Nomic model within its 512-token request context."""

    def __init__(self, *args, request_pause: float = 0.1, **kwargs):
        super().__init__(*args, **kwargs)
        self._request_pause = request_pause

    def _embed_chunk(self, chunk: str) -> list[dict]:
        try:
            vector = self.embed_text("search_document: " + chunk)
            if len(vector) != 768:
                raise ValueError(f"expected 768 embedding dimensions, got {len(vector)}")
            return [{"text_chunk": chunk, "vector": vector}]
        except Exception as error:
            if "too large to process" not in str(error).lower() or len(chunk) < 2:
                raise
            midpoint = len(chunk) // 2
            return self._embed_chunk(chunk[:midpoint]) + self._embed_chunk(chunk[midpoint:])
        finally:
            if self._request_pause:
                time.sleep(self._request_pause)

    def get_chunks_and_embeddings(self, document) -> list[dict]:
        return [embedding for chunk in split_text(document.page_content)
                for embedding in self._embed_chunk(chunk)]


def verified_paths(repo: Path) -> list[Path]:
    """Reject incomplete, changed, duplicate, or unlisted source files."""
    directory = repo / RELATIVE_DIR
    manifest = directory / "manifest.tsv"
    paths = []
    seen_numbers = set()
    seen_ids = set()
    with manifest.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != EXPECTED_COLUMNS:
            raise ValueError("unexpected BlueWorld manifest columns")
        for row in reader:
            number = int(row["article_number"])
            if number < 1 or number in seen_numbers or row["message_id"] in seen_ids:
                raise ValueError(f"duplicate or invalid article in manifest: {number}")
            seen_numbers.add(number)
            seen_ids.add(row["message_id"])
            path = directory / f"alt.games.everquest-pre2000-{number:06d}.txt"
            raw = path.read_bytes()
            if len(raw) != int(row["bytes"]) or hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ValueError(f"article {number} differs from manifest")
            if row["utf8_valid"] != "yes":
                raise ValueError(f"article {number} is not UTF-8; text handler cannot read it")
            raw.decode("utf-8")
            paths.append(path)
    if len(paths) != EXPECTED_COUNT:
        raise ValueError(f"expected {EXPECTED_COUNT} BlueWorld articles; found {len(paths)}")
    if set(directory.glob("*.txt")) != set(paths):
        raise ValueError("BlueWorld article files and manifest do not match")
    return paths


def index_paths(repo: Path, paths: list[Path], indexer: Indexer,
                es_manager: ElasticsearchManager, limit: int = 0) -> tuple[int, int]:
    client = es_manager.get_client()
    indexed = 0
    skipped = 0
    for position, path in enumerate(paths[:limit] if limit else paths, 1):
        relative_path = path.relative_to(repo).as_posix()
        if client.exists(index=es_manager._index_name, id=relative_path):
            skipped += 1
            continue
        docs = indexer._get_docs(relative_path, str(path))
        if len(docs) != 1 or docs[0]["id"] != relative_path:
            raise ValueError(f"unexpected generated documents for {relative_path}")
        doc = docs[0]
        if not doc["text_full"] or not doc["text"]:
            raise ValueError(f"empty text or embeddings for {relative_path}")
        doc["domain_name"] = "archive.usenet.blueworldhosting.com"
        response = client.index(index=es_manager._index_name, id=relative_path,
                                op_type="create", document=doc)
        if response.get("result") != "created":
            raise ValueError(f"indexing did not create {relative_path}")
        indexed += 1
        if position % 100 == 0:
            LOGGER.info("Processed %s/%s articles (%s created, %s existing)",
                        position, len(paths), indexed, skipped)
    return indexed, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(os.environ.get("LOCAL_REPO_PATH", "/data/eq-archives")))
    parser.add_argument("--limit", type=int, default=0, help="index at most this many articles for a probe")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("limit cannot be negative")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    paths = verified_paths(args.repo)
    LOGGER.info("Verified %s recovered articles", len(paths))
    if args.verify_only:
        return 0
    os.environ["SKIP_LLM_ENRICHMENT"] = "true"
    os.environ["LOCAL_REPO_PATH"] = str(args.repo)
    es_manager = ElasticsearchManager(manage_index=False)
    embedder = BoundedArchiveEmbedder()
    indexer = Indexer(es_manager, embedder)
    indexed, skipped = index_paths(args.repo, paths, indexer, es_manager, args.limit)
    LOGGER.info("Finished: %s created, %s existing", indexed, skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
