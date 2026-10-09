"""Create-only, manifest-directed indexing of approved and published captures.

No Git checkout, finder, RabbitMQ, mapping updates or existing-document edits.
Reuse the archive's extractor, Nomic chunker and default source-bound enrichment.
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
from captures import check_manifest, indexable, verified_source
from capture_budget import budget_identity
from indexer.chunking import CHUNKING_VERSION, DOCUMENT_PREFIX, chunk_source
from indexer.html_extraction import WEBSITE_EXTRACTION_VERSION, website_markdown
from indexer.capture_enrichment import Enricher, policy
from import_status import EnrichmentError, describe, write_status


def read_batch(root, relative, expected):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise CrawlError("Invalid or oversized approved manifest path")
    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise CrawlError("Invalid or oversized approved manifest path")
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
    content = (website_markdown(header) + '\n\n' + text if capture.get('content_type', '').split(';')[0] == 'text/plain'
               else website_markdown(header + text))
    if not content.strip():
        raise CrawlError("Capture has no readable source text")
    return replay, content


class Services:
    def __init__(self, directory=Path("/run/secrets"), enricher=None):
        self.enricher = enricher
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
        if self.enricher:
            self.enricher.close()

    def enrich(self, capture, source):
        if not self.enricher:
            raise CrawlError("Default enrichment is not configured; document remains pending")
        return self.enricher.enrich(capture, source)

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
    policy(manifest)
    created = skipped = preserved = 0
    for capture in manifest["captures"]:
        if not indexable(capture):
            preserved += 1
            continue
        record_id = capture["archive_path"]
        if services.exists(record_id):
            skipped += 1
            continue
        replay, content = text_document(root, capture)
        # Model date extraction sees the original body, not our synthetic
        # provenance header or the capture timestamp embedded in its URL.
        source, _ = decode(verified_source(root, capture), capture.get("content_type") or "")
        metadata = services.enrich(capture, source if capture.get('content_type', '').split(';')[0] == 'text/plain' else website_markdown(source))
        chunks = [{"text_chunk": chunk.text, "vector": services.embedding(chunk.text)} for chunk in chunker(content)]
        if not chunks:
            raise CrawlError("Capture has no source chunks")
        summary_vectors = [services.embedding(chunk.text) for chunk in chunker(metadata["llm_summary"])]
        if not summary_vectors:
            raise CrawlError("Summary has no usable embedding chunks; enriched result retained for retry")
        summary_vector = [sum(vector[index] for vector in summary_vectors) / len(summary_vectors) for index in range(768)]
        magnitude = math.sqrt(sum(value * value for value in summary_vector))
        if not magnitude:
            raise CrawlError("Summary embedding is empty; enriched result retained for retry")
        summary_vector = [value / magnitude for value in summary_vector]
        timestamp = datetime.strptime(capture["timestamp"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
        document = {"id": record_id, "title": capture.get("title") or urlsplit(capture["url"]).path or "Archived page",
                    "domain_name": urlsplit(capture["url"]).netloc, "capture_date": timestamp, "last_indexed": now(),
                    "url": replay, "mime_type": (capture.get('content_type') or 'text/html').split(';')[0], "file_type": "text", "thumbnail": "thumbnails/website.webp",
                    "text_full": content, "text": chunks, "text_extraction_version": WEBSITE_EXTRACTION_VERSION,
                    "text_chunking_version": CHUNKING_VERSION, "archive_source_sha256": capture["sha256"],
                    "archive_source_manifest": batch["manifest_sha256"], "archive_commit": batch["publication"]["commit"],
                    **metadata, "llm_summary_vector": summary_vector}
        if services.create(record_id, document):
            created += 1
        else:
            skipped += 1
    return {"created": created, "existing": skipped, "captures": len(manifest["captures"]),
            **({'preserved_files': preserved} if preserved else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--enrichment-root", type=Path, default=Path("/enrichment"))
    args = parser.parse_args()
    services = None
    enricher = None
    directory = None
    job_name = os.environ.get('IMPORT_JOB_NAME', '')
    def report(state, error=None):
        if directory is not None:
            try:
                write_status(directory, job_name, args.manifest_sha256, state, error)
            except (OSError, ValueError):
                print('Import status could not be saved; consult this Job log.', flush=True)
    try:
        batch = read_batch(args.root, args.batch, args.manifest_sha256)
        settings = policy(batch["manifest"])
        if not re.fullmatch(r'[a-f0-9]{32}', batch['manifest']['batch_id']):
            raise CrawlError('Invalid import batch identity')
        directory = args.enrichment_root / batch['manifest']['batch_id']
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        budget_id = budget_identity(args.root, batch['manifest'])
        enricher = Enricher(args.enrichment_root / budget_id, "/run/secrets/luna_api_key",
                            maximum=settings["max_enrichment_usd"])
        report('running')
        services = Services(enricher=enricher)
        print(json.dumps(index_batch(args.root, batch, services)), flush=True)
        report('completed')
    except (CrawlError, OSError, httpx.HTTPError) as error:
        report('failed', error)
        detail = str(error) if isinstance(error, EnrichmentError) else describe('indexing')
        print('Capture indexing failed: ' + detail + ' No existing documents were overwritten.', flush=True)
        return 1
    finally:
        if services:
            services.close()
        elif enricher:
            enricher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
