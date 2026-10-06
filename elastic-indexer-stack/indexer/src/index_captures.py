"""Create-only, manifest-directed indexing of approved and published captures.

No Git checkout, finder, RabbitMQ, mapping updates, paid LLM or enrichment edits.
Reuse the archive's Markdown extractor and checksum-pinned Nomic chunker.
"""

import argparse
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from common import CrawlError, decode, digest, now
from captures import check_manifest, verified_source
from indexer.chunking import CHUNKING_VERSION, DOCUMENT_PREFIX, chunk_source
from indexer.html_extraction import WEBSITE_EXTRACTION_VERSION, website_markdown


def read_batch(root, relative, expected):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if root not in path.parents or path.stat().st_size > 2 * 1024 * 1024:
        raise CrawlError("Invalid or oversized approved manifest path")
    try:
        batch = json.loads(path.read_text())
        manifest = batch["manifest"]
        commit = batch["publication"]["commit"]
    except (OSError, ValueError, KeyError, TypeError):
        raise CrawlError("Published batch manifest is unavailable or invalid") from None
    if batch.get("manifest_sha256") != expected or digest(manifest) != expected or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise CrawlError("Batch does not match the published and approved manifest")
    check_manifest(root, manifest)  # Verify the entire file set before any ES writes.
    return batch


def text_document(root, capture):
    text, _ = decode(verified_source(root, capture), capture.get("content_type") or "")
    replay = f"https://web.archive.org/web/{capture['timestamp']}/{capture['url']}"
    header = f"<b>Page URL:</b> {html.escape(replay)}<br/><hr/>"
    content = website_markdown(header + text)
    if not content.strip():
        raise CrawlError("Capture has no readable source text")
    return replay, content


class Services:
    def __init__(self, directory=Path("/run/secrets")):
        self.es_url = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch.eqarchives-es.svc.cluster.local:9200").rstrip("/")
        self.index = os.environ.get("ELASTICSEARCH_INDEX", "eq-archive")
        self.model = os.environ.get("EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5@q8_0")
        self.embedding_url = os.environ.get("EMBEDDING_URL", "http://nomic-embeddings.eqarchives-es.svc.cluster.local:8080").rstrip("/")
        self.client = httpx.Client(auth=(directory.joinpath("es_username").read_text().strip(),
                                        directory.joinpath("es_password").read_text().strip()),
                                   timeout=httpx.Timeout(120, connect=10), trust_env=False)
        self.embedder = httpx.Client(headers={"Authorization": "Bearer " + directory.joinpath("openai_api_key").read_text().strip()},
                                    timeout=httpx.Timeout(120, connect=10), trust_env=False)

    def close(self):
        self.client.close()
        self.embedder.close()

    def exists(self, record_id):
        response = self.client.head(f"{self.es_url}/{quote(self.index, safe='')}/_doc/{quote(record_id, safe='')}")
        if response.status_code not in (200, 404):
            raise CrawlError(f"Elasticsearch HTTP {response.status_code}; upstream details omitted")
        return response.status_code == 200

    def embedding(self, text):
        for attempt in range(3):
            try:
                response = self.embedder.post(self.embedding_url + "/v1/embeddings", json={"model": self.model, "input": DOCUMENT_PREFIX + text})
            except httpx.HTTPError:
                if attempt == 2:
                    raise CrawlError("Nomic request failed; batch remains resumable") from None
            else:
                if response.status_code == 200:
                    try:
                        vector = response.json()["data"][0]["embedding"]
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise CrawlError("Nomic returned an invalid embedding") from None
                    if (not isinstance(vector, list) or len(vector) != 768 or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
                            or not any(vector)):
                        raise CrawlError("Nomic returned an invalid 768-dimensional vector")
                    time.sleep(0.2)
                    return vector
                if response.status_code not in (429, 502, 503, 504):
                    raise CrawlError(f"Nomic HTTP {response.status_code}; upstream details omitted")
            time.sleep(2 * (attempt + 1))
        raise CrawlError("Nomic is busy; batch remains resumable")

    def create(self, record_id, document):
        response = self.client.put(f"{self.es_url}/{quote(self.index, safe='')}/_create/{quote(record_id, safe='')}", json=document)
        if response.status_code == 409:
            return False  # Another writer won; preserve its document/enrichment.
        if response.status_code != 201 or response.json().get("result") != "created":
            raise CrawlError(f"Elasticsearch create failed (HTTP {response.status_code}); upstream details omitted")
        return True


def index_batch(root, batch, services, chunker=chunk_source):
    manifest = batch["manifest"]
    check_manifest(root, manifest)
    created = skipped = 0
    for capture in manifest["captures"]:
        record_id = capture["archive_path"]
        if services.exists(record_id):
            skipped += 1
            continue
        replay, content = text_document(root, capture)
        chunks = [{"text_chunk": chunk.text, "vector": services.embedding(chunk.text)} for chunk in chunker(content)]
        if not chunks:
            raise CrawlError("Capture has no source chunks")
        timestamp = datetime.strptime(capture["timestamp"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
        document = {"id": record_id, "title": capture.get("title") or urlsplit(capture["url"]).path or "Archived page",
                    "domain_name": urlsplit(capture["url"]).netloc, "capture_date": timestamp, "last_indexed": now(),
                    "url": replay, "mime_type": "text/html", "file_type": "text", "thumbnail": "thumbnails/website.webp",
                    "text_full": content, "text": chunks, "text_extraction_version": WEBSITE_EXTRACTION_VERSION,
                    "text_chunking_version": CHUNKING_VERSION, "archive_source_sha256": capture["sha256"],
                    "archive_source_manifest": batch["manifest_sha256"], "archive_commit": batch["publication"]["commit"]}
        if services.create(record_id, document):
            created += 1
        else:
            skipped += 1
    return {"created": created, "existing": skipped, "captures": len(manifest["captures"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    services = None
    try:
        batch = read_batch(args.root, args.batch, args.manifest_sha256)
        services = Services()
        print(json.dumps(index_batch(args.root, batch, services)), flush=True)
    except (CrawlError, OSError, httpx.HTTPError):
        print("Capture indexing failed; no existing documents were overwritten. Retry the verified manifest.", flush=True)
        return 1
    finally:
        if services:
            services.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
