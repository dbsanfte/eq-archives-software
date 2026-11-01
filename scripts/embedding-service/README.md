# Standalone CUDA embeddings for indexing

The Ubuntu GPU host runs the **same EmbeddingGemma 2 Q8_0 weights,
768-dimensional normalized vectors, document/query prefixes and model alias** as
the Vulkan service. All indexers use CUDA; live search and MCP use Vulkan.
The separately managed CUDA server is reachable on the wired LAN only, at
`192.168.50.193:8081`, with the existing Nomic API key. It is not a paid API.
The root Kubernetes Kustomization manages a selectorless
`nomic-embeddings-rtx3080` Service/Endpoints pointing to this host, without changing
the Vulkan Deployment. The JSON patch removes the Service selector added by
Kustomize's shared labels; the rendered-route CI check verifies that routing stays
on the external address and does not select local cluster pods. Reserve the laptop's wired DHCP address in the router;
update both `cuda.env` and the endpoint manifest if its address changes.

## Host prerequisites and installation

Use Ubuntu's signed NVIDIA server driver compatible with the host kernel and
Secure Boot, Docker, and NVIDIA Container Toolkit with **native CDI** support.
This installation uses driver 580.178.04, toolkit 1.20.1 and Docker 29.1.3.
Enable `nvidia-cdi-refresh.path`; `docker run --device nvidia.com/gpu=all` must list
the CUDA GPU. Keep Secure Boot enabled. Do not raise the laptop's 80 W power limit.
The CUDA image in `cuda.env` is pinned to the same llama.cpp 1052f7e1d626 revision as
`k8s-manifests/embeddings/runtime.env`. That existing file remains authoritative
for model URL, SHA-256 and alias. Consult the
[Ubuntu driver guide](https://ubuntu.com/server/docs/nvidia-drivers-installation/)
and [NVIDIA CDI guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html).

Provision the shared **Nomic** key privately at
`/etc/eqarchives-embeddings/openai_api_key` (root-owned parent mode 0700).
Never use the paid Luna key here. Run from a software checkout on the GPU host:

```bash
sudo bash scripts/embedding-service/install.sh
sudo systemctl status eqarchives-embeddings.service
sudo journalctl -u eqarchives-embeddings.service -n 30
```

The script installs configuration in `/opt/eqarchives-embeddings`, verifies or
fetches the pinned model into `/var/lib/eqarchives-embeddings/models`, and enables
the systemd service and inference health timer. The container is non-root,
read-only, drops capabilities and has only model/key/cache mounts. Its key is
never passed in arguments. Docker failures restart after 15 seconds; three
consecutive failed one-minute inference checks restart a stalled server.
Automatic CDI refresh avoids stale GPU configuration after reboot/driver updates.
The installer enables persistence in `nvidia-persistenced` and orders CDI after
that daemon. Ubuntu's packaged daemon otherwise starts with persistence disabled;
on this laptop, CDI closing the last GPU handle during boot hung in the driver's
ACPI shutdown path. Keep these drop-ins when updating the NVIDIA packages.
A kernel-level GPU deadlock can require a physical power cycle; container restart
recovery alone cannot repair a wedged kernel driver.
To upgrade, rerun installation and explicitly restart the service after checking
new pins; installation does not restart an already running service automatically.

For an operator-authorized always-on laptop, configure logind with
`HandleLidSwitch=ignore`, `HandleLidSwitchExternalPower=ignore`,
`HandleLidSwitchDocked=ignore`, `IdleAction=ignore` in a drop-in under
`/etc/systemd/logind.conf.d/`, and mask sleep, suspend, hibernate, hybrid-sleep and
suspend-then-hibernate targets. Keep AC power connected and ventilation clear.

## Capacity and validation

The measured profile has 64 request slots, 512 tokens per input, and a **512-token
compute batch/microbatch**. Large physical batches reduced throughput severely
for this noncausal encoder. On the RTX 3080 Laptop GPU at its stock 80 W limit,
representative archive inputs reached about 120 chunks/second with eight inputs
per request and 2–4 concurrent requests. Increasing concurrency further did not
improve the measured embedding throughput. These are embedding-only measurements;
actual document rates also depend on source extraction, chunking and ES writes.

Cross-backend cosine similarity on eight real archive chunks was 0.9995–0.9997;
the operator accepted these numerical differences. Same-device serial/batch
results with the 512-token compute profile were identical for those inputs.
The default cross-backend check requires 0.999 cosine; the threshold is explicit
and configurable. Do not treat a model/alias/dimension mismatch as numerical noise.

Keep token-checked private fixtures and measurement outputs outside Git. The
benchmark bounds duration, request count and concurrency, refuses public API
endpoints, validates complete indexed finite vectors, and reuses HTTP connections:

```bash
python3 scripts/embedding-service/benchmark.py \
  --url http://192.168.50.193:8081/v1/embeddings \
  --api-key-file /private/nomic-api.key \
  --model 'text-embedding-nomic-embed-text-v1.5@q8_0' \
  --fixtures /private/prefixed-480-token-chunks.json \
  --batch-size 8 --concurrency 4 --seconds 15 --encoding base64
python3 -m unittest discover -s scripts/embedding-service -v
```

Check near-limit, final partial batches, oversize rejection/recovery, unauthorized
requests, service restart and host reboot before routing production work.

## Indexer policy and handoff

`NOMIC_EMBEDDING_PROFILE` defaults to `vulkan`, retaining the existing one/two-input
limit. Explicit `cuda` permits 1–64 inputs. `NOMIC_EMBEDDING_ENCODING=base64`
transports the same float32 vectors with less JSON overhead; `float` stays the
default. Both formats require complete returned indices and 768 finite values
before a document can be saved. The 480-token chunker and version checkpoints
remain unchanged.

`REINDEX_WORKERS=4` starts isolated handler/embedding clients for the primary CUDA
endpoint. The production renderer removes all inherited `REINDEX_SECONDARY_*`
settings and sets `OPENAI_ENDPOINT_EMBEDDINGS` explicitly to CUDA, so a previous
embedding override cannot send background indexing to Vulkan. The four workers
share one bounded ES scan and read-only source tree. Curation imports and saved
indexing Job templates also use the CUDA endpoint. Public search and MCP retain
their Vulkan endpoint independently of laptop availability.
`REINDEX_PREPARATION_WORKERS=2` opts into a separate, bounded process pool for
HTML extraction and pinned-tokenizer chunking, avoiding contention on Python's
interpreter lock. It defaults to zero for existing Jobs. Spawned processes read
sources only, have no ES/model clients or enrichment, and return complete text
and source-bound chunk offsets. The parent validates coverage before submitting
embeddings. HTTP requests and optimistic ES updates remain in the original
bounded workers. One document stays wholly on its assigned backend. Optimistic partial updates
retain existing enrichment, dates, OCR, provenance and checkpoint versions.

Use the [operator handoff sequence](../../elastic-indexer-stack/indexer/k8s/README.md)
only after the protected PR passes and merges. Verify the predecessor image ID,
run the complete legacy pytest/90% coverage gate against that environment with
the changed sources overlaid, then build a small dependency-preserving update:

```bash
docker build -f scripts/embedding-service/Dockerfile.reindex \
  --build-arg BASE_IMAGE=eqarchives-indexer:<verified-running-image-tag> \
  --tag eqarchives-indexer:rtx3080-<merged-40-character-sha> .
```

Save the original Job JSON/logs privately and render a **pending** replacement:

```bash
python3 scripts/embedding-service/render-reindex.py \
  --name reindex-cuda-<date> \
  --image eqarchives-indexer:rtx3080-<merged-40-character-sha> \
  < /private/running-job.json > /private/new-job.json
```

Import the image into k3s, server-side dry-run, probe exact remaining IDs and verify
metadata/vectors. Create the replacement at parallelism zero before suspending the
old Job. Wait for the old pod to stop, then raise replacement parallelism to one.
Keep the original recovery runner, immutable ConfigMap, credentials, previous Jobs,
logs and prepared source checkout; never repeat source preparation. Curation's
unfinished-Job guard remains in effect throughout. Watch both backend counters,
GPU utilization/power and ES resource usage. No LLM enrichment or API spend is added.

## EmbeddingGemma migration

The production text embedding model is EmbeddingGemma 2 Q8_0, 768 dimensions,
canonical alias `embeddinggemma-2-768-v1`. CUDA and Vulkan image pins use the same
llama.cpp revision (1052f7e1d626); public model/runtime hashes live in `runtime.env`
and `cuda.env`. Both GPUs fit the same 310 MB text model. The separate multimedia
encoders are not needed for this text/OCR migration; no image/audio backfill is
implied. Never mix Gemma and Nomic vectors in the same semantic query.

1. Validate both pinned runtimes privately, including finite normalized vectors,
   cross-host cosine, near-480-token batch/serial identity, oversize rejection and
   recovery. Preserve the prior systemd runtime/configuration and model files.
2. Test `indexer/test` on Python 3.13 with **both** pinned tokenizers installed.
   Build the operator image using `Dockerfile.reindex` and the exact previously
   verified image as `BASE_IMAGE`. Tag it `eqarchives-indexer:rtx3080-<merged-sha>`;
   record its immutable local image ID, and import it to k3s containerd.
3. Save the old Job JSON and logs privately. Render a new Job with
   `render-reindex.py --gemma --name reindex-gemma-<date> --image <tested-image>`.
   It starts with `parallelism=0`, retaining the source checkout, credentials,
   recovery wrapper and no-paid-enrichment policy. Create it **before** suspending
   the old Job, so the curation unfinished-Job guard remains effective.
4. After normal CI/deployment completes, suspend the old broad Job as an explicit
   operator handoff. Wait for its pod to stop; do not delete the Job, repeat source
   preparation or allow Nomic/Gemma writers to overlap. Install the new CUDA
   `runtime.env`, `cuda.env` and `service.py` and restart the embedding service.
   Verify authenticated inference and the systemd health check before proceeding.
5. Run exact-ID migration probes with the tested image against CUDA, including a
   long text, existing summary/OCR and an old extraction version. Inspect complete
   vectors and the optimistic update, and verify keyword plus model-filtered
   semantic searches. Probes must not regenerate Luna enrichment.
6. Set the prepared new Job's `parallelism=1`. Use four isolated network workers,
   two CPU preparation processes and `GEMMA_EMBEDDING_BATCH_SIZE=8` with profile
   `cuda`, initially retaining the benchmarked 64-slot/512-token server profile.
   There is no intentional request sleep. Read-only sources remain at
   `/mnt/samsung/eq-archives-html-reindex`; current extraction versions reuse stored
   full text to avoid source I/O. Curation also routes exclusively to this CUDA
   endpoint, using Gemma's prefix/tokenizer and marker on newly imported documents.

`reindex_embeddings.py` scans stable IDs without a long-lived PIT. It preserves
source text, existing enrichment/OCR, dates and provenance; summaries/OCR are
chunked and normalized when aggregated. One optimistic partial update replaces
**every searched vector** and then marks the model version. Invalid/incomplete
vectors cannot mark readiness; conflicts and document errors are counted and do
not hold other workers. Missing sources are counted, retain original text/chunks,
and never claim repaired extraction. Unusable old vectors with no corresponding
text are cleared during replacement. Empty sources are not invented.

`embedding_model` is the resume checkpoint, independently of historical extraction
and chunking versions. The recovery wrapper waits for infrastructure and resumes
completed versions after temporary outages. A final nonzero failed count requires
inspection and another checkpointed pass; never report it as complete. To check
progress, count remaining documents using `remaining_query()` and count the exact
`embedding_model` marker; read rate/counters from the Job's bounded progress logs.
Do not equate a Job exit or a process-local processed count with total completion.

The frontend and MCP use Gemma query prefixes and restrict all semantic branches
to the marker. Keyword search still covers the entire index. Deployment validates
that a zero-ready initial corpus really has zero model checkpoints; once ready
records exist, the vector-only smoke check must return a match. Test search
relevance on the completed corpus before claiming an improvement. Preserve old
models/Jobs for diagnosis; reverting runtime alone after partial migration is
unsafe because the embedding spaces differ even though both have 768 dimensions.
