# Search relevance benchmark

This operator tool compares search configurations using the actual frontend's
frozen Search UI request builder, query-syntax policy and capture identity code.
It does not change frontend defaults, index mappings or archive documents.
Elasticsearch transport permits only metadata reads, searches, and opening or
closing a point in time (PIT). Keep it outside the frontend Kustomization.

## What is measured

`queries.json` contains 60 hand-written research queries, including exact names,
questions, mechanics, historical research, acronyms, ambiguity, typos, lexical
constraints, source/date filters and negative controls. Forty queries are for
tuning and twenty are reserved for validation. These are seed information needs,
not recorded user traffic or proof that answers exist. Add exact ES document IDs
to `known_relevant_ids` when a researcher knows targets; the collector includes
them even when every configuration misses them. Targets must exist in the fixed
cohort and satisfy source/date restrictions.

`configs.json` contains explicit experiments, not recommended defaults: current
lexical/hybrid, title weighting, source vectors alone, larger retrieval depths,
candidate counts, boosts, a similarity threshold and application-side RRF.
Native Elasticsearch RRF is not needed. Explicit query syntax retains the
frontend's lexical constraints in every experiment, including semantic-only.

The collector creates one short-lived PIT after caching query embeddings. It
uses the same snapshot for every configuration, randomizes configuration order,
prefilters every semantic branch, scans the frontend's 50-record metadata batches
within its 1,000-capture bound, and retains grouped and raw rankings. Complete
indexed source text is collected in the same PIT; summaries are never substituted
for that evidence. The PIT is closed on success or failure and expires after two
minutes without another request. The default 15-minute run budget prevents an
accidental multi-day snapshot while reindexing is active.

The default cohort requires `embeddinggemma2-paragraphs-480-overlap48-v1`. While regeneration
is unfinished this cohort can be highly biased toward the sources processed
first. Inspect the recorded source distribution. A mail-heavy pilot cannot
establish website capture-grouping performance, historical Usenet coverage or
the best defaults for the completed archive. Repeat with `--chunking-version ''`
on the completed index and reserve fresh queries if the original validation set
has already influenced additional experiments.

## Setup and collection

Python 3.10+ standard library, Docker and the frontend's Node 22/Yarn lockfile are
sufficient. Python 3.13 is used for verification. Build only its dependencies:

```bash
docker build --target dependencies --tag eqarchives-benchmark-node elastic-indexer-stack/frontend
python3 -m unittest discover -s scripts/search-benchmark -v
```

The bridge runs in a network-disabled container. Its three frontend integration
tests run with the regular Jest/coverage gate and exercise native request
construction, conservative capture identity and the date picker range contract.

On eqvm, forward the existing services in separate terminals:

```bash
sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n eqarchives-es \
  port-forward --address 127.0.0.1 service/elasticsearch 19200:9200
sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n eqarchives-es \
  port-forward --address 127.0.0.1 service/nomic-embeddings 18080:8080
```

Prepare a mode-0600 JSON credential file outside Git from trusted runtime secrets:
`es_username`, `es_password`, `embedding_api_key`. Prefer an operator read account
with `read` and `view_index_metadata` on the index;
an existing administrative account is not required for document collection.
Never put credentials in URLs, arguments, checked-in examples or output.
For TLS, pass `--es-ca-file` with the operator-owned combined CA bundle; certificate
and hostname verification remain enabled. A PIT reader needs `read` and
`view_index_metadata` on both the alias and its resolved concrete index, plus
cluster `monitor` for recorded version metadata. This remains a read-only account.

Use a new private output directory for each retrieval run. Keep the raw files,
source bodies, vectors, requests and model responses outside Git. Record the
dedicated archive checkout's committed SHA with `--archive-sha`:

```bash
python3 scripts/search-benchmark/benchmark.py run \
  --es-url http://127.0.0.1:19200 \
  --embedding-url http://127.0.0.1:18080/v1 \
  --credentials /private/benchmark-credentials.json \
  --archive-sha '<archive-commit-sha>' \
  --out /private/search-pilot
```

`--query-ids q007,q043,q045 --config-ids current-hybrid,source-semantic,title-lexical`
selects a small smoke run. `--embedding-cache` can reuse query vectors across
runs; its key includes the model and complete model-specific prefixed input (Gemma uses `task: search result | query: `). The default
request pause is 0.2 seconds and inference is serial for the shared Nomic slot.
There is no LLM work during collection. Each retrieval run is capped at 800
query/configuration cases; partial shards, timeouts and escaped filters fail it.
Smoke runs collected with `--depth` below 50 do not report recall@50.
Optional `--include-passages` saves only the fixed text/title/context/kind/offset
fields for the [private reranker pilot](../search-reranker/README.md), with complete
readers and source hashes in the same PIT. It never requests vectors. The default
source projection remains unchanged; the manifest records this opt-in.
Do not score failed runs. A rerun needs a new PIT and output directory.

The manifest records the software SHA and source hashes, actual frontend
defaults, model, ES version, mappings, settings, archive SHA, extraction/chunking
versions, cohort counts/distribution and run times. `requests.json` preserves the
ranking DSL; the collector supplies the current PIT handle and pagination per
batch. Timings cover sequential HTTP searches under the observed cluster load,
excluding browser painting, facets, highlights and cold embedding time. This is
not an end-to-end browser performance test. Repeated/warm runs and a completed
index are needed before choosing a latency budget.

## Judgments and human calibration

Export an offline, blind review page:

```bash
python3 scripts/search-benchmark/benchmark.py review --run /private/search-pilot
```

Open `review.html` locally. It shows complete indexed source text and the stated
information need, with configuration names and ranks hidden. Grade 0 irrelevant,
1 tangential, 2 useful, 3 directly relevant; download `human-ratings.jsonl`.
The page keeps progress in browser local storage and contains no credentials or
vectors. Do not serve the credentials directory with a general HTTP file server.

Optional paid grading uses the public Responses API and **`gpt-6-luna`** only:

```bash
python3 scripts/search-benchmark/benchmark.py grade \
  --run /private/search-pilot --api-key-file /private/public-openai-key
```

The API key is separate from Nomic's key and read from its file without logging.
Requests use structured output and `store: false`; shuffled batches contain only
the information need and complete source text, never rankings, scores or generated
summaries. Missing/oversized sources are left unjudged for human review rather
than truncated. Per-pair source/query/prompt hashes make grading resumable. The
default character budget is 20 million across all attempts; exceeding it stops
with existing judgments retained. Usage and a recorded pricing estimate are
saved separately, including cached input and reasoning/output tokens.
The default first screen grades the union of top-10 raw/grouped results from all
configurations. It supports NDCG/precision/MRR@10 while leaving deeper results
explicitly unjudged. Rerun `grade --depth 50` to extend the same source-bound
judgments for pooled recall@50; already graded pairs are reused.
See [the model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna)
and [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

Calibrate model judgments on a stratified human sample, especially disagreements,
low-confidence grades, negatives, quoted replies and exact entity matches.
Human judgments override model grades when both files are supplied. Do not
describe model judgments or another AI's review as human validation.

## Scoring and selection

```bash
python3 scripts/search-benchmark/benchmark.py report \
  --run /private/search-pilot --ratings /private/search-pilot/human-ratings.jsonl

# Explicitly opt into a provisional model-judged report.
python3 scripts/search-benchmark/benchmark.py report \
  --run /private/search-pilot --allow-model-ratings \
  --ratings /private/search-pilot/model-ratings.jsonl /private/search-pilot/human-ratings.jsonl
```

`report.json` and `report.md` contain raw/grouped NDCG@10 and NDCG@50, precision@10, MRR@10,
**pooled** recall@50, judgment coverage, per-query regressions, category results,
latency and paired bootstrap intervals on validation queries. Recall's denominator
is the judged pool, not every relevant document in the archive. Unjudged top-10
results suppress top-10 metrics; unjudged top-50 results suppress recall@50.
NDCG@50 also requires all returned top-50 results to be judged and a collection
depth of at least 50. Unknown judgments are not silently graded irrelevant.
Queries with no positives in the pool have undefined NDCG/recall and are counted
through precision/coverage. Grading zero returned results does not prove the
corpus lacks an answer. Pool additional configurations and known targets when
making stronger claims about recall.

The tune winner uses only queries scorable across **all** configurations. The
reserved validation set does not select it. Compare its validation difference
with `current-hybrid`, inspect regressions by query/source, and consider latency.
The separate **observed average ranking** orders configurations by mean grouped
NDCG@10 over all common scorable queries, including validation. It names every
tied winner and saves the resolved lexical/vector fields, kNN parameters and
similarity/RRF settings using the retrieval manifest's recorded defaults. This
describes the best observed average on this query suite; it is not an independent
validation result and does not replace the tuning/validation split.
Do not deploy a global default from a model-judged, incomplete-corpus pilot.
Changing production defaults is a separate protected PR with frontend regression
and browser checks.

## Approximate neighbour diagnostic

```bash
python3 scripts/search-benchmark/benchmark.py ann \
  --run /private/search-pilot \
  --es-url http://127.0.0.1:19200 --embedding-url http://127.0.0.1:18080/v1 \
  --credentials /private/benchmark-credentials.json
```

This samples 512 regenerated documents in a fresh PIT, stores their original
vectors privately, computes exact cosine similarity per chunk and takes the
maximum per parent, matching nested kNN's ranking semantics. It compares native
kNN for several candidate counts and reports both strict ID overlap and a
tie-aware measure for duplicate vectors. It is a separate snapshot from the
relevance run. Elasticsearch can switch a small filtered sample to brute force:
this diagnostic does **not** establish full-index HNSW recall or prove that a
particular candidate count is sufficient. A larger isolated replica with exact
reference rankings is the next step if approximate recall remains a concern.
See [Elastic's kNN documentation](https://www.elastic.co/guide/en/elasticsearch/reference/8.17/knn-search.html).

## Portal and MCP calibration with top-20 reranking

`calibration.py` measures the browser adapter's **normalized** requests and MCP's
hybrid `search` requests against one full-index PIT. The browser comparison groups
captures/discussions before reranking; MCP `search` preserves its document results.
This does not turn the filtered `search_archive` tool into a hybrid search.
`calibration-queries.json` reserves 24 fresh validation questions separately from
24 tuning questions; these are authored research needs, not sampled user traffic.
`calibration-configs.json` fixes thirteen retrieval experiments. Each is evaluated
with Qwen weights 0, 0.5, 0.75 and 1 in a reciprocal-position blend (constant 10).
Only the same top-20 window can move; unsupported source positions remain fixed.
The expanded suite compares independent ANN breadth, cosine floors of 0.50/0.65,
`minimum_should_match: 2<-25%` on unconstrained lexical queries, and application
RRF of keyword/source-vector ranks (100-record window, rank constant 60). RRF is
computed privately and requires no subscription feature. Collection is bounded
at 1,400 query/configuration/interface cases and 15 minutes for its PIT.
The baseline is `current-r1`, including the already deployed Qwen reranker.

Use the MCP Python environment and its pinned `tokenizers` dependency. Build the
Node bridge and prepare read-only credentials as above. Keep all output private:

```bash
PYTHONPATH=elastic-indexer-stack/mcp python scripts/search-benchmark/calibration.py collect \
  --out /private/calibration/retrieval \
  --queries scripts/search-benchmark/calibration-queries.json \
  --configs scripts/search-benchmark/calibration-configs.json \
  --credentials /private/benchmark-credentials.json \
  --es-url https://elasticsearch:9200 --es-ca-file /private/ca-bundle.pem \
  --embedding-url http://embeddings:8080/v1 --archive-sha '<archive-commit-sha>'

PYTHONPATH=elastic-indexer-stack/mcp python scripts/search-benchmark/calibration.py rank \
  --run /private/calibration/retrieval --url http://private-reranker:8083 \
  --tokenizer /private/qwen-tokenizer.json --key /private/reranker-key

# Paid calls: require an explicitly authorized total dollar budget.
python scripts/search-benchmark/calibration_judge.py \
  --run /private/calibration/retrieval --split tune \
  --key /private/openai-key --max-usd 20
python scripts/search-benchmark/calibration_report.py freeze \
  --run /private/calibration/retrieval
python scripts/search-benchmark/calibration_judge.py \
  --run /private/calibration/retrieval --split validation \
  --key /private/openai-key --max-usd 20
python scripts/search-benchmark/calibration_report.py validate \
  --run /private/calibration/retrieval
```

The dollar cap is shared across both splits in the parent directory's private
SQLite ledger. Requests reserve conservative input/output charges before sending;
unknown outcomes retain their reservations and are not silently retried. Completed
responses are cached durably, with actual token usage and estimated charges.
Judging uses `gpt-6-luna`, low reasoning effort, 32,768 output tokens, structured
results and `store: false`. At most three judging requests run concurrently;
GPU scoring remains serial. Ranking configurations and scores are hidden from
the judge. The pooled top-10 sources are graded in full, using OCR only when no
reader text exists. Sources exceeding 900,000 UTF-8 bytes stay explicitly unjudged.

Freeze chooses each interface's tuning winner once, before validation judgments
exist. Validation checks that the frozen manifest and tuning evidence are unchanged.
Report paired NDCG@10 confidence intervals, precision, missing judgments and
individual regressions. These are **model judgments**, not human validation or
exhaustive corpus recall. Negative controls with no relevant pooled documents have
undefined NDCG and remain visible through precision. Full-index snapshots can
contain mixed passage-policy versions: disclose their recorded distribution.
Cached partial Qwen timings are not full-query latency; separately time complete
top-20 requests and verify the actual production paths before deploying a change.

Additional controls follow Elastic's [8.19 kNN documentation](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/knn-search.html),
[term matching](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/query-dsl-minimum-should-match.html)
and [RRF](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/rrf.html).
BM25 `k1`/`b`, analyzers and HNSW construction settings affect index configuration
and remain outside this read-only experiment. A cosine floor applies to vector
matches only, not a global relevance cutoff for hybrid lexical matches.
`--embedding-cache /private/previous/embedding-cache` reuses exact model/query
inputs. `calibration_judge.py --reuse-ratings /private/previous/ratings-tune.jsonl`
reuses only exact query/source/judge hashes; changed evidence is judged again.
Basic MCP `search` retains native score ties; filtered `search_archive` retains
its deterministic research sort. The collector must not substitute one for the other.

## November 2026 calibration

The 13 November 2026 experiment used a PIT containing **4,860,847 records** on
Elasticsearch 8.19.24. Thirteen retrieval configurations and four Qwen weights
produced 52 combinations for each interface. Twenty-four authored queries were
used for tuning, and 24 fresh queries for held-out validation. Both tuning winners
were frozen before validation grading. The baseline already included production
Qwen top-20 reranking; these gains are additional to that deployment.

| Interface | Frozen selection | Validation NDCG@10: baseline → selected | Useful results P@10: baseline → selected | Paired NDCG change, bootstrap 95% interval |
| --- | --- | --- | --- | --- |
| Portal, grouped captures/discussions | Keyword/source-vector RRF, window 100, constant 60; source k=100/candidates=500; pure Qwen top-20 | 0.6634 → **0.7142** (+7.7% relative) | 0.7542 → 0.7542 | +0.0508 [0.0080, 0.0895] |
| MCP `search`, document results | Four-field hybrid, k=50/candidates=250, vector boost 30; pure Qwen top-20 | 0.6081 → **0.6803** (+11.9% relative) | 0.6625 → **0.7583** | +0.0722 [0.0003, 0.1467] |

Tuning NDCG@10 was 0.6978 → 0.7657 for the portal and 0.5716 → 0.6780
for MCP. The expanded pool contained 3,611 distinct documents; all 1,311 tuning
and 1,252 validation query/source pairs required for top-10 comparisons were
judged without missing or oversized sources. Luna judging cost **$1.252938**
(590 requests, including the initial collection; 11,848,940 input and 136,088
output tokens), against an explicitly authorized $20 cap. No uncertain charges
remain. Complete readers, prompts, responses, rankings, source hashes, ledgers
and frozen selection receipts stay in private operator storage, outside Git.

These are **gpt-6-luna model judgments**, not human validation. NDCG uses graded
0–3 relevance and a pooled ideal; P@10 counts grades 2–3 as useful. The two negative
controls had no useful documents, but some tangential grade-1 judgments made their
NDCG mathematically defined. Excluding those controls, the 22 answer-seeking
queries still improved: portal 0.7237 → 0.7698 (paired change +0.0461,
95% interval [0.0011, 0.0900]); MCP 0.6634 → 0.7421 (+0.0788,
[0.0003, 0.1610]). These additional figures do not select or retune either winner.

Improvements included “Singing Steel Boots” and “SOW indoors” in the portal,
and “rune spell aggro” and necromancer-to-cleric mana transfer in MCP. Meaningful
regressions remain: portal “Velious faction armour Kael Thurgadin” (-0.189 NDCG),
MCP “enchanter charisma lull resist” (-0.119) and “Coldain shawl tailoring skill”
(-0.414). Do not present the average gain as an improvement for every question.

Sequential retrieval-only timing over the 48 queries was portal median/p95
361/1,273 ms for the baseline versus 630/1,162 ms for RRF; MCP 148/353 ms versus
156/344 ms for boost 30. These exclude embeddings, Qwen, browser painting and
facets. Six complete top-20 production reranker probes, including source retrieval,
took 441–743 ms without fallback. Three subsequent built-portal probes against
the real archive took 2.96–4.75 seconds to the first page, including cold-browser
startup, embeddings, both retrieval legs, union facets, Qwen and rendering.
These checks covered mobile and desktop, matched the evaluator's fusion and
grouped top-20 IDs, and verified disjoint next pages and stable back navigation.
Three MCP runtime probes verified all four branches at k=50/candidates=250/boost=30,
then successful top-20 reranking and its exact top-ten output in 0.77–1.10 seconds.
These small probes are functional checks, not production latency percentiles.

The snapshot included 3,841,698 v10 passage records, 649,897 v12 records and
369,252 without a text passage-policy marker. Policy maintenance continued during
this work; no indexing Job, archive document or mapping was changed by calibration.
These authored English text-research queries are not a traffic sample, an image
retrieval evaluation, or exhaustive corpus recall. Re-evaluate with fresh held-out
questions as the corpus and passage policies change. The query pool, source hashes
and frozen paired results support reproducibility, not a claim of universal gains.

The live portal's RRF prefix is followed by a deduplicated lexical browsing tail.
Every evaluated query supplied at least 20 groups in its fixed fusion prefix, so
the browsing tail does not alter the measured top-20 candidate set. Explicit
constraints/date sorts retain their native paths. The collector now exports the
portal policy with its frontend contract and resolves `current` per interface;
future runs must use their own recorded baseline rather than treating this
historical comparison as their current deployment.
