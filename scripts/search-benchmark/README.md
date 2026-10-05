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

The default cohort requires `nomic-paragraphs-480-overlap48-v1`. While regeneration
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
runs; its key includes the model and complete `search_query:` input. The default
request pause is 0.2 seconds and inference is serial for the shared Nomic slot.
There is no LLM work during collection. Each retrieval run is capped at 800
query/configuration cases; partial shards, timeouts and escaped filters fail it.
Smoke runs collected with `--depth` below 50 do not report recall@50.
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
