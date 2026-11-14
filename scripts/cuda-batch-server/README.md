# CUDA batch inference

Private experimental text inference for the RTX 3080 Laptop GPU (SM86). This is
an additive service: it does not replace the embedding, vision or reranking
containers, change the search API, or restart indexing Jobs.

**Production cutover is gated on GPU correctness, resource checks and paired
HTTP measurements. A successful build alone does not establish a speedup.**

## Execution

Each transformer layer executes in one cooperative CUDA kernel launch,
including normalization, Q/K/V projection, RoPE, attention, residuals and the
feed-forward network. Gemma layers also include their per-layer embedding path.
There are 24 layer launches for EmbeddingGemma 2 and 28 for Qwen3-Reranker 0.6B.
Input preparation and the final pooling/classification head use PyTorch. CUDA
graphs are not used as a substitute for layer fusion.

Tensor Core matrix accumulators stay in registers. Aligned matrix tiles use
vectorized global loads/stores and shared memory. Intermediate tensors that
cross cooperating blocks use bounded batch workspaces. Full attention is exact
for the configured text bounds; no KV cache or cross-request prefix cache is
retained. The service loads the existing pinned Q8_0 GGUF files and validates
their hashes and tokenizer hashes in `backend.py`.

The reranker uses NVIDIA CUTLASS's device-side matrix-multiply main loop inside
the layer kernel. Its [pinned source](https://github.com/NVIDIA/cutlass/tree/ad7b2f5e84fcfa124cb02b91d5bd26d238c0459e)
is fetched and hash-checked only during the build. The [BSD notice](third_party/CUTLASS-LICENSE.txt)
is retained in the source and runtime image. Gemma uses the local WMMA path.
Both paths dequantize the pinned Q8_0 weights to FP16 and use FP32 accumulators
and residuals. Arithmetic differs from llama.cpp's Q8 execution; score agreement
and retrieval quality must be evaluated independently of token/weight identity.

The asynchronous HTTP server has independent embedding and reranking queues,
worker threads and CUDA streams. Each model has one owner for GPU submission;
multiple clients feed that model's bounded dynamic batches. Length buckets and
a 4,096 padded-token scheduling quantum keep large imports from producing an
unbounded GPU batch. Exactly identical complete token sequences share inference
within one batch, with every original response index preserved.

The causal reranker also shares the exact, tile-aligned token prefix within a
batch. Dense projections and feed-forward work for that prefix execute once;
every document retains its own positions, attention and final score. This does
not retain a cache across batches and is disabled for bidirectional Gemma.
GPU validation compares shared and separate execution, including uneven lengths.
An explicit library ABI check rejects stale binaries before launching a layer.

When both models are active, cooperative-grid sizes are bounded to leave room
for the other stream. The limit is reconsidered at each layer. Shared GPU
resources still impose a tradeoff: overlap must be measured, and is not a
promise of increased combined throughput. A CUDA error marks both workers
unhealthy because they share a CUDA context. Cancellation removes pending work;
an already executing batch finishes safely before its buffers are reused.

## HTTP contract

The container listens on port 8080. The installer publishes host port 8084 on
loopback and the explicitly supplied `192.168.0.0/16` LAN address. It supplies no
public ingress. Both inference routes require their operator-owned bearer key.
Keys are read from files and are never included in command arguments or logs.

| Route | Contract |
| --- | --- |
| `POST /v1/embeddings` | OpenAI-compatible embeddings, model `embeddinggemma-2-768-v1`, 1–64 inputs, at most 512 tokens each, 768 dimensions, float or base64 output |
| `POST /v1/rerank` | Qwen/llama.cpp-compatible reranking, model `qwen3-reranker-0.6b-q8-v1`, query and 1–120 documents, at most 768 tokens per complete prompt |
| `GET /health` | Readiness and build revision; unavailable during startup, GPU failure or a stalled worker |

Reranking is not an OpenAI-standard endpoint. Its response carries every original
document index and a finite relevance score. The embedding API accepts strings
or token arrays; callers retain the archive's existing query/document task
prefixes. Inputs are rejected if oversized, never silently truncated. Image
embeddings continue to use the separate vision companion.

The server accepts only these routes and known request fields. It limits uploads
to 256 KiB/two seconds, inference waiting to ten seconds, the full request
lifecycle to fifteen seconds and serialized responses to 2 MiB. Each model has
eight HTTP admission slots held through final response delivery, so bulk uploads
or slow readers cannot consume the other model's reservation. Invalid Unicode,
duplicate JSON fields, nonfinite values, unknown models and incomplete outputs
fail explicitly. Error bodies omit upstream/internal details.

## Verification

CPU tests do not require model weights or CUDA:

```bash
python3 -m unittest discover -s scripts/cuda-batch-server -v
bash -n scripts/cuda-batch-server/install.sh
```

The tools image contains nvcc, Nsight Systems, Nsight Compute and
compute-sanitizer. `build_kernels.py` compiles the selected profiles for SM86 and
retains compiler reports, disassembly, defines and source/binary hashes.
`check_kernels.py` rejects any spill, stack frame, local-memory use, mismatched
kernel inventory or missing Tensor Core instructions. Do not suppress failures
or accept a spill merely to increase occupancy.

On the GPU host, use private model/report directories and an isolated container.
Keep existing inference services and active Kubernetes Jobs running:

```bash
python verify_gpu.py --kind embed --batch 8 --length 512 --oracle \
  --output /private/embed-check.json
python verify_gpu.py --kind rerank --batch 8 --length 768 --oracle \
  --output /private/rerank-check.json
python verify_concurrency.py --output /private/concurrency.json
```

Also check uneven lengths, multiple requests, cancellation, maximum input
lengths and a valid request after invalid input. Run compute-sanitizer memcheck,
racecheck and initcheck against the selected binaries. Nsight capture windows
are available through `--profile`; inspect the kernel timeline to verify both
the full-layer launch count and cross-stream overlap. Profile counter access,
if needed, belongs only to the isolated profiling process, never the serving
container or a global host policy change.

For initialization checks, instrument the complete forward pass, including
PyTorch's setup and output kernels. Filtering only `layer_forward` excludes the
producers of its tensors and makes the initialization results invalid. For example:

```bash
compute-sanitizer --tool initcheck --error-exitcode 70 \
  python verify_gpu.py --kind rerank --repeats 1 --output /private/initcheck.json
```

Timing under a sanitizer is diagnostic overhead, not serving performance. The
synthetic stress inputs use distinct document labels so prefix sharing does not
turn a large-batch check into repeated copies of one identical input.

`benchmark_http.py` accepts the private source-linked passage evidence fixture
from the search benchmark. It alternates backend order, uses distinct concurrent
queries and follows the live reranking client's exact-prompt deduplication,
20-document batches and four shared batch slots. It records complete response
identity, latency distributions, embedding cosine agreement and ranking/score
differences. `--diagnostic` records failed gates; it does not turn them into
passes. Generated passages, vectors, raw model data and credentials stay
outside Git. Reusing existing model judgments requires no new paid API calls;
such judgments are not human validation.

`benchmark_batching.py` compares the **same two complete reranking requests**
run serially and concurrently. Its primary metric is elapsed time until both
requests finish; individual request latencies and output agreement are retained.
It alternates execution order and reports paired speedups with a diagnostic
bootstrap interval. Both HTTP benchmarks retain the live client's 20-document,
128 KiB batch bounds and four shared batch slots. A small fixture cannot establish
a universal performance guarantee.

## Additive installation and rollback

After GPU and HTTP gates pass, build the runtime image from the reviewed source
revision. The runtime contains neither the compiler/profilers nor model files.
Use an immutable local image ID and exact Git SHA:

```bash
docker build --build-arg GIT_SHA="$(git rev-parse HEAD)" \
  --tag eqarchives-cuda-batch:validated scripts/cuda-batch-server
sudo scripts/cuda-batch-server/install.sh \
  "$(docker image inspect --format '{{.Id}}' eqarchives-cuda-batch:validated)" \
  "$(git rev-parse HEAD)" 192.168.68.108
```

The installer checks the image revision, verifies and hardlinks existing models,
downloads only hash-pinned tokenizers when absent, and installs dedicated
`eqarchives-cuda-batch` systemd units. The container is non-root, read-only,
capability-free and resource bounded. Readiness failures trigger a restart of
this service only. Existing endpoint credentials remain unchanged.

Point an opted-in client at the new endpoint only after measuring its actual
batch/concurrency workload. The installation does not edit any client URL or
indexing Job. To roll back, restore that client's previous endpoint and stop
`eqarchives-cuda-batch-health.timer` and `eqarchives-cuda-batch.service`. The old
services, their models, keys and persistent indexing checkpoints remain available.
