# Indexing backend development

The shared extraction, chunking and model clients support several ingestion paths:

- **Curation imports:** approved, published site manifests run as create-only
  Kubernetes Jobs, with default Luna enrichment and persistent budget accounting.
- **Policy maintenance:** bounded, resumable slices upgrade existing passages and
  embeddings, yielding to new imports and preserving enrichment and provenance.
- **Operator Jobs:** explicit model migrations, source repairs and image backfills
  have their own source verification, cursors and recovery procedures.
- **Legacy RabbitMQ workers:** a file finder queues archive files for consumers.

Start with the [curation guide](../curation/README.md),
[Jobs guide](k8s/README.md) and
[embedding runbook](../../scripts/embedding-service/README.md) for current production
workflows. RabbitMQ is required only for the legacy finder/consumer path below.
A software deployment does not start a broad reindex or replace existing Jobs.

## Legacy finder and consumer setup

The legacy queue-based backend has two process modes: `--file-finder` discovers
work, and `--indexer` consumes it. Running them requires:

- Python 3.13, Git, and the system `libmagic` library, as used by the
  [indexer Dockerfile](Dockerfile).
- Elasticsearch 8.x with an indexing account allowed to create the index
  template and index, and read and write documents.
- RabbitMQ reachable by both processes.
- A shared, writable archive checkout, with the same `LOCAL_REPO_PATH` for both
  processes.
- An OpenAI-compatible embedding endpoint and, for enrichment, text or vision
  models. The current Elasticsearch mappings use **768-dimensional vectors**.

For a Linux development environment, install the system dependencies above, then
install the Python dependencies and run the tests:

```bash
cd elastic-indexer-stack/indexer  # from the repository root
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r src/indexer/requirements-dev.txt
PYTHONPATH=src python -m indexer.chunking --download-tokenizer /tmp/nomic-tokenizer.json
PYTHONPATH=src python -m indexer.gemma --download-tokenizer /tmp/gemma-tokenizer.json
GEMMA_TOKENIZER_PATH=/tmp/gemma-tokenizer.json NOMIC_TOKENIZER_PATH=/tmp/nomic-tokenizer.json PYTHONPATH=src python -m pytest src/indexer/test
```

The Docker build downloads the pinned tokenizer automatically.

The dependency set includes document conversion, OCR, and machine-learning
libraries, so installation is substantially larger than the frontend setup.
Run the full legacy backend suite separately. CI also tests the extraction and
import code used by the curation image.

Configure the processes using environment variables:

| Variable | Purpose |
| --- | --- |
| `GIT_REPO_URL` | Archive repository to clone or update; defaults to `dbsanfte/eq-archives` on GitHub |
| `LOCAL_REPO_PATH` | Shared checkout directory; defaults to `/data/eq-archives` |
| `SPARSE_CHECKOUT_PATHS` | Optional comma-separated archive subdirectories to fetch |
| `SLEEP_INTERVAL` | Seconds between file-finder scans; defaults to `3600` |
| `RABBITMQ_HOST`, `RABBITMQ_PORT`, `RABBITMQ_QUEUE` | Queue connection; defaults to `rabbitmq`, `5672`, and `file_paths` |
| `ELASTICSEARCH_HOST`, `ELASTICSEARCH_PORT` | Elasticsearch URL without port, and separate port; defaults to `http://elasticsearch` and `9200` |
| `ELASTICSEARCH_INDEX` | Destination index; defaults to `eq-archive` |
| `OPENAI_ENDPOINT` | Model API base URL, including `/v1` |
| `OPENAI_ENDPOINT_COMPLETIONS`, `OPENAI_ENDPOINT_EMBEDDINGS` | Optional separate API base URLs overriding `OPENAI_ENDPOINT` |
| `OPENAI_EMBEDDING_MODEL_NAME` | Embedding model; use the same model for indexing and frontend queries |
| `OPENAI_TEXT_MODEL_NAME`, `OPENAI_IMAGE_MODEL_NAME` | Models used for text and image enrichment |
| `SKIP_LLM_ENRICHMENT` | Set to `true` to skip summaries, tags, and other enrichment; text indexing still generates embeddings |
| `NOMIC_TOKENIZER_PATH` | Optional path to the checksum-pinned Nomic tokenizer; the image contains it at `/opt/eqarchives/nomic-tokenizer.json` |
| `NOMIC_EMBEDDING_BATCH_SIZE` | Legacy Nomic inputs per request; default `1`, up to `2` on the Vulkan profile or `64` with `NOMIC_EMBEDDING_PROFILE=cuda` |
| `SKIP_TEXT_FILES`, `SKIP_IMAGE_FILES`, `SKIP_OTHER_FILES` | Set individual flags to `true` to omit file types |
| `REINDEXING_ENABLED`, `REINDEXING_INTERVAL` | Enable periodic reindexing and set the interval in seconds |
| `LOG_LEVEL` | Logging level; defaults to `INFO` |

Mount indexing credentials at `/run/secrets/es_username`,
`/run/secrets/es_password`, and `/run/secrets/openai_api_key`. For local processes,
`ELASTICSEARCH_USERNAME`, `ELASTICSEARCH_PASSWORD`, and `OPENAI_API_KEY` can instead
be supplied through the environment. Keep their values out of Git and shell
history. Both modes initialize the model clients, so configure the API base URLs
and key for the file finder as well as the indexer.

With dependencies and configuration in place, run each mode in a separate
terminal from `elastic-indexer-stack/indexer`:

```bash
python src/main.py --file-finder
```

```bash
python src/main.py --indexer
```

Index mappings are defined in
[`es_manager.py`](src/indexer/es_manager.py).
The template matches `eq-archive*`; use a matching index name or adapt the
template. Keep the frontend's `indexName`, the public API and NGINX `ELASTICSEARCH_INDEX` settings, and
the index or alias containing the indexed documents consistent.

The existing Compose file is a deployment reference with environment-specific
addresses and zero replicas for the backend services. Adapt the addresses,
secrets, index names, and replica counts before using it for your own stack.
Elasticsearch and enrichment model servers must be provisioned separately.
Production live query embeddings prefer reserved capacity on the
[CUDA batch service](../../scripts/cuda-batch-server/README.md), with bounded
Vulkan fallback described in the [deployment guide](../k8s-manifests/README.md).
Current curation imports and policy maintenance use the CUDA batch service's
bulk queue, behind coequal browser and MCP work.

For the project overview, see the [root README](../../README.md).
