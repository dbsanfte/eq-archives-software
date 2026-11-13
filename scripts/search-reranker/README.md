# Private Qwen reranker pilot

This is an independent **experimental** llama.cpp service on eqvm's loopback
`127.0.0.1:8083`, with its own API key and systemd unit. Neither the public search
portal nor MCP calls it. It does not change Elasticsearch documents, embeddings,
ranking defaults, existing Kubernetes Jobs or the CUDA/Vulkan embedding services.
The Vulkan profile shares the physical integrated GPU with live query embeddings;
measure contention before connecting it to production search.

`runtime.env` pins the existing Vulkan image, source model, converter revision,
converted Q8_0 file hash and capacity. The 0.6B source is Apache-2.0 licensed:
[Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B).
The model ranks a query/document pair using the yes/no classification head;
loading a generative GGUF is not evidence of correct relevance scoring.

## Model preparation and installation

Keep model weights, sources, dependencies, API keys and benchmark evidence outside
Git and the archive checkout. Use an isolated conversion environment; never
upgrade dependencies inside running indexers. Download the official source at
`SOURCE_REVISION` from `runtime.env`, including its README and tokenizer files.
The source `model.safetensors` SHA-256 is
`27cd75a405b9c1b46b59abfd88aaa209e6fed2a1972cde9b70e7659537c5e65b`;
the tokenizer SHA-256 is
`aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`.

Use the upstream converter at the pinned `CONVERTER_REVISION` with its conversion
requirements. The pilot used Python 3.13, CPU PyTorch 2.6.0, Transformers 4.57.6,
Tokenizers 0.22.2 and SentencePiece 0.2.1. Name the source directory
`Qwen3-Reranker-0.6B` so the converter identifies the reranker and emits its
two-label `cls.output.weight`, rank pooling and the correct prompt template:

```bash
python convert_hf_to_gguf.py /private/Qwen3-Reranker-0.6B \
  --outtype q8_0 --outfile /private/models/qwen3-reranker-0.6b-q8_0.gguf
```

The resulting file must match `MODEL_SHA256` before installation. Verify known
positive/negative pairs against the original source model before adopting any
new conversion/runtime pin. The initial pilot also checks Japanese text. Compare
ranking separation and numeric drift; probabilities are not calibrated relevance
grades and small quantization differences are expected.

Generate a dedicated random API key in a private file, without printing it or
reusing either Luna's paid key or the embedding key. After the protected PR is
merged, install only this independent service:

```bash
sudo bash scripts/search-reranker/install.sh \
  /private/models/qwen3-reranker-0.6b-q8_0.gguf /private/reranker-api-key
sudo python3 /opt/eqarchives-reranker/service.py health
systemctl status eqarchives-reranker-pilot.service
```

The installer verifies the model, installs under `/opt/eqarchives-reranker`,
`/var/lib/eqarchives-reranker/models`, and `/etc/eqarchives-reranker`, then enables
`eqarchives-reranker-pilot.service`. It must not restart other services. After
deliberate configuration updates restart only this unit and rerun inference
health. Stop the unit to roll back the pilot; live search continues unchanged.
`DEVICE=none` selects an isolated CPU profile without exposing the render device.
CI tests the service configuration/client/ordering; actual installation is an
explicit host operation, like the separate CUDA embedding service.

## Bounded offline search experiment

First collect the actual frontend's `current-hybrid` configuration with
[`scripts/search-benchmark`](../search-benchmark/README.md), using
`--chunking-version ''` for the full current corpus rather than its old pilot
cohort. Keep the 60-query tune/validation split. Use a dedicated read-only ES
account with cluster `monitor` and index `read,view_index_metadata` on the index
alias **and its resolved concrete index**, as PIT searches need the latter.
Use the existing verified combined CA profile; never disable TLS validation.
Preserve a short-lived PIT and its original deadline. Close it before inference.

The saved `sources.json` must include the exact, source-bound text passages
(`text.text_chunk`, `text.title`, `text.context`, `text.kind`, `text.source_start`,
`text.source_end`) alongside complete reader/OCR text, without any vectors. The
benchmark verifies each saved source hash. Input selection uses lexical overlap
among indexed passages, and a reader/OCR prefix only when chunks are absent.
This selector is a pilot limitation, especially for conceptual searches. Every
scoring excerpt records its original passage hash, selector and shortening flag;
the complete reader stays available in the private retrieval evidence.

Install Tokenizers 0.22.2 in an isolated environment and use the pinned source
tokenizer. Run serial experiments while ingestion remains active:

```bash
python scripts/search-reranker/benchmark.py \
  --run /private/retrieval --out /private/qwen-top20 \
  --api-key-file /private/reranker-api-key \
  --tokenizer /private/Qwen3-Reranker-0.6B/tokenizer.json \
  --candidates 20 --passages 1 --tokens 512
```

The client accepts only a loopback endpoint, forbids redirects, bounds request and
response bytes, and rejects duplicate/missing indices, wrong models and nonfinite
scores. It checks the complete prompt budget including title/context/query and
template, keeps exact source IDs, stable ties and unsupported image/empty-source
positions, and leaves the ranking tail unchanged. Candidate windows are 10, 20 or
50, at most two passages each; all source/query/model identities remain explicit.
Reports preserve separate baseline and reranked orders with p50/p95 inference
latency and ranking changes. They **do not claim relevance improvement**.

Use the existing blind review workflow or separately authorized judgments before
reporting NDCG/precision or changing ranking defaults. Inspect EQ names, mechanics,
forum attribution, historical constraints, quotes, negative queries, Japanese and
image/OCR results. Record live-embedding latency and ingestion progress under the
same load. Start with 20 candidates; test larger windows only when their measured
latency is useful. No Luna grading is implicit in this command.

```bash
python3 -m unittest discover -s scripts/search-reranker -v
bash -n scripts/search-reranker/install.sh
```
