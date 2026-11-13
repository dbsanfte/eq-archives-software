# Initial Qwen reranker pilot — 13 November 2026

These measurements establish serving correctness and latency. They do **not**
establish a corpus-wide relevance gain, and production rankings remain unchanged.

## Setup

- Official Qwen3-Reranker-0.6B weights converted to Q8_0 with the pinned converter
  and runtime in `runtime.env`; dedicated private API key and loopback endpoints.
- PN50: Ryzen 7 4700U integrated Radeon through Vulkan. Its shared GPU memory
  loaded the model successfully without firmware or memory-allocation changes.
- CUDA: RTX 3080 Laptop GPU, 16 GiB, existing stock 80 W profile. The reranker ran
  separately from the existing embedding and vision services.
- One 60-query retrieval snapshot using the actual frontend `current-hybrid`
  request builder, source/date/phrase constraints and capture/discussion grouping.
  Retrieval covered 4,855,828 indexed records, including supporting assets, and
  saved 3,520 pooled sources. The PIT was closed before reranking.
- Chunking maintenance was still active: this snapshot mixes v10/v12 and
  unversioned assets. It is not the final-policy corpus benchmark.
- Each candidate contributed one indexed passage selected by lexical overlap;
  title/context/query/template were included in the 512-token scoring limit.
  Complete reader text remains in the private evidence. About one quarter of
  selected passages required an explicitly recorded scoring excerpt.

## Measured reranking latency

| Backend | Candidates | Queries | Scored pairs | Median | 95th percentile |
| --- | ---: | ---: | ---: | ---: | ---: |
| PN50 Vulkan | 20 | 60 | 1,200 | 9.05 s | 11.08 s |
| RTX 3080 CUDA | 20 | 60 | 1,200 | 0.54 s | 0.66 s |
| RTX 3080 CUDA | 50 | 60 | 3,000 | 1.33 s | 1.54 s |

Timings cover the inference HTTP request/response. CUDA includes the private SSH
tunnel across the LAN. They exclude candidate retrieval, passage selection,
process/source loading and browser painting. All requests were serial at the
client, with two inference slots. Results include concurrent ingestion load;
they do not establish a public multi-user capacity or latency guarantee.

Nine reference cases, including Japanese, were checked against the original
PyTorch model using the same prompt. Maximum probability differences were
0.0138 for Vulkan and 0.0037 for CUDA. Every tested response retained complete,
unique document indices and finite scores. These checks validate scoring, not
the model's EQ expertise or probability calibration.

A small 12-request probe of the **live embedding endpoint**, using identical
input, measured median latency of 119 ms during the Vulkan reranking run and
34 ms after that test stopped. This is not a complete user-facing search benchmark,
but supports keeping the heavier reranker away from the live Vulkan service.

## Qualitative source inspection

The assistant inspected selected source passages and observed useful promotions:

- `Fungi Covered Scale Tunic`: a full item/stat/drop entry moved from fifth to
  first, ahead of a mostly empty item-heading fragment.
- Bard song twisting: a direct explanation of twisting multiple songs moved
  from seventh to first, ahead of a broader songweaving guide excerpt.
- `monk weight limit`: a level-by-level weight-limit table moved from eighth to
  first, ahead of a complaint about overweight armour.

These are selected qualitative examples, not independent human judgments or a
measured precision/NDCG improvement. Some changes were ambiguous: a broad
`charm breaks` query promoted a patch-message discussion over practical charm
tactics. The lexical passage selector and incomplete chunk-policy catch-up also
limit conclusions. Ranking changed for most queries; change alone is not quality.

The promising first integration target is **20 candidates on CUDA**, with a
bounded deadline and baseline-ranking fallback. Before enabling it, perform
blind relevance review, check exact names/quotes/negatives and historical filters,
measure multi-user latency and ingestion contention, and design stable grouped
pagination shared by portal and MCP. Larger windows should justify their extra
latency through relevance evidence. No paid Luna judging was used in this pilot.
