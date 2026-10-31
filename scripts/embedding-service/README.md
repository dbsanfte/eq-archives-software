# Standalone CUDA embeddings and dual-GPU reindex

The optional Ubuntu GPU host runs the **same Nomic Embed v1.5 Q8_0 weights,
768-dimensional normalized vectors, document/query prefixes and model alias** as
the default Vulkan service. Public search and MCP retain the default endpoint.
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
The CUDA image in `cuda.env` is pinned to the same llama.cpp b11096 revision as
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
endpoint. `REINDEX_SECONDARY_ENDPOINT` adds one independently configured Vulkan
worker (two inputs, float encoding), controlled by `REINDEX_SECONDARY_WORKERS`.
At most eight workers total share one bounded ES scan and read-only source tree.
The next free worker takes the next record, naturally allocating work in proportion
to device throughput; no fixed 50/50 split or slow-worker barrier between pages.
The log's `primary_updated` and `secondary_updated` counters show the actual split.
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
  --name reindex-dual-gpu-<date> \
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
