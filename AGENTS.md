# Automated development guide

These instructions apply throughout this repository. Follow the user's current
request, preserve unrelated work, and keep this guide consistent with the actual
workflows and deployment scripts when those change.

## Project and source of truth

This public repository, `dbsanfte/eq-archives-software`, contains the frontend and
indexing backend for https://search.eqarchives.org. The license is
**AGPL-3.0-only**; preserve the existing Elastic third-party notices. Archive
content lives in the separate `dbsanfte/eq-archives` repository.

All paths and commands below are relative to the repository root unless stated
otherwise. Consult the linked files for settings rather than copying stale image
digests, dependency versions, or credentials into new files.

| Subject | Authoritative files |
| --- | --- |
| Architecture, backend setup and environment variables | [README.md](README.md) |
| Frontend development, container configuration and browser tests | [Frontend guide](elastic-indexer-stack/frontend/README.md) |
| Public MCP contract, tests, limits and connection instructions | [MCP guide](elastic-indexer-stack/mcp/README.md), [public connection screen](elastic-indexer-stack/frontend/public/mcp.html) |
| Deployment, prerequisites, credentials and rollback | [Deployment guide](elastic-indexer-stack/k8s-manifests/README.md) |
| Required checks and production delivery | [Frontend CI/CD](.github/workflows/elastic-indexer-stack-cicd.yml) |
| Frontend build and coverage configuration | [Dockerfile](elastic-indexer-stack/frontend/Dockerfile), [package.json](elastic-indexer-stack/frontend/package.json), [Jest configuration](elastic-indexer-stack/frontend/jest.config.js) |
| Production reconciliation and runtime secrets | [deploy-frontend.sh](scripts/deploy-frontend.sh), [frontend-secrets.py](scripts/frontend-secrets.py) |
| Intranet curation and targeted imports | [Curation guide](elastic-indexer-stack/curation/README.md), [deployment](scripts/deploy-curation.sh), [secret provisioning](scripts/curation-secrets.py) |
| Managed Kubernetes resources | [Root Kustomization](elastic-indexer-stack/k8s-manifests/kustomization.yaml), [frontend manifests](elastic-indexer-stack/k8s-manifests/01-frontend.yaml), [embedding manifests](elastic-indexer-stack/k8s-manifests/embeddings/deployment.yaml) |
| Model/image pins, bootstrap and GPU allocation | [runtime.env](elastic-indexer-stack/k8s-manifests/embeddings/runtime.env), [embedding Kustomization](elastic-indexer-stack/k8s-manifests/embeddings/kustomization.yaml), [device plugin](elastic-indexer-stack/k8s-manifests/embeddings/device-plugin.yaml) |
| Indexer dependencies, tests and mappings | [Indexer Dockerfile](elastic-indexer-stack/indexer/Dockerfile), [test requirements](elastic-indexer-stack/indexer/src/indexer/requirements-dev.txt), [tests](elastic-indexer-stack/indexer/src/indexer/test), [Elasticsearch manager](elastic-indexer-stack/indexer/src/indexer/es_manager.py) |
| Search relevance evaluation and tuning | [Benchmark guide](scripts/search-benchmark/README.md), [query set](scripts/search-benchmark/queries.json), [experiments](scripts/search-benchmark/configs.json) |
| Website candidate discovery, Wayback staging and approval | [Crawler guide](scripts/archive-crawler/README.md), [seed sites](scripts/archive-crawler/seeds.json), [pinned downloader](scripts/archive-crawler/vendor/wayback-machine-downloader/UPSTREAM.json) |

## Branching and delivery

1. Inspect `git status`, fetch `origin`, and start a descriptive branch from the
   current `origin/master`, such as `fix/mobile-filters` or `feat/search-options`.
   Do not overwrite another person's edits or include unrelated files.
2. Make the change and its regression tests together. Stage specific files,
   inspect the staged diff for secrets and unintended changes, and run
   `git diff --check` before committing.
3. Push the branch and create a pull request targeting `master`. Describe the
   resulting behavior and the checks actually performed. For multiline PR text,
   use a temporary file with `gh pr create --body-file`.
4. Keep the PR up to date with `master` and wait for the exact required check,
   **`Test and build frontend`**, to pass for the current revision before merging.
   Normal changes must use this protected PR path; never fabricate check results.
5. When deployment is part of the task, follow the subsequent `master` workflow
   through deployment and verify the live behavior before reporting completion.

`master` protection requires a PR, an up-to-date branch, and that GitHub Actions
check. It also applies to administrators. No additional reviewer approval is
required. Force pushes and branch deletion are disabled. Preserve these settings
for normal development; do not weaken them to get a failing change merged.

Use `[skip ci]` only when the user explicitly requests a documentation-only
exception. It skips push/PR workflows, including deployment, but does **not**
satisfy required checks or bypass branch protection. A skipped PR check remains
pending. Keep any explicitly authorized administrative exception limited to that
commit and restore the existing protections immediately afterward. A skipped
documentation commit does not change the live build SHA. See
[GitHub's skip behavior](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs).

Use the authenticated `gh` CLI for repository operations. The VM has an older
version; REST calls are a reliable fallback for unsupported flags:

```bash
gh api repos/dbsanfte/eq-archives-software/branches/master/protection
gh api 'repos/dbsanfte/eq-archives-software/actions/runs?branch=master&per_page=5' \
  --jq '.workflow_runs[] | {id,head_sha,status,conclusion,html_url}'
```

Public search security uses the bounded browser adapter in
`elastic-indexer-stack/mcp/eqarchives_mcp/web_api.py`, retaining cached-browser
URLs without forwarding arbitrary DSL. Preserve closed grammar/cumulative query
budgets, exact routes/methods, fixed source/highlight projections, finite vectors,
shared admission limits, streamed body/response bounds, deadlines and sanitized
errors. NGINX receives no backend credentials and runs non-root/read-only on 8080.
Keep reader projections nonempty and bounded. Apply one conservative analyzed-text
budget across main queries, URL phrases, filters, facets and kNN pre-filters;
whitespace alone is not a token bound. Reject malformed Unicode and oversized
numeric inputs before upstream work. Invalid caller input must not change shared
embedding cooldown; preserve backoff for actual service/auth/rate failures.
MCP uses the same analyzed-text policy as browser search. Retain its two process-wide
HTTP slots through response delivery, 16 KiB/two-second uploads, 30-second lifecycle
and 8 MiB serialized output limit; reject oversized output explicitly without
truncating source text. Deliver MCP/browser bodies in 64 KiB writes with a final
drain so Uvicorn backpressure retains admission through transport delivery.
Keep an explicit protocol-method allowlist and tools-only
discovery in both protocol eras so SDK defaults cannot enable subscription streams.
Test slow sockets, cancellation, deadlines, chunked output and modern discovery.
The real slow-reader smoke test runs inside the isolated API container with
bounded TCP send buffers, so Docker's forwarded-port buffers cannot absorb its
entire fixture body and conceal transport backpressure. Keep proxy/routing tests
separate and retain the same busy/recovery assertions.
Keep snippets as safe text/em elements and summaries as text, plus the CSP.
Run the MCP tests, public-security manifest checks, real Chromium regressions and
`check-public-security.py`; deploy the API before frontend and isolation policies
after old frontend pods retire. Only the Elasticsearch Service is CI-managed;
its StatefulSet/data and active reindex Jobs remain untouched. Database TLS/version
or multi-minor k3s/runtime upgrades require an operator maintenance handoff.
Retain the operator-owned `eqarchives-database-client` endpoint/combined CA profile
on every deployment; bootstrap only when absent and never disable TLS validation.
Run `python3 scripts/test-database-client-profile.py` for transport/deploy changes.

## Testing requirements

**Every bug fix needs a regression test.** Reproduce the failure before the fix
where practical, then verify that the same test passes afterward. Test observable
behavior rather than merely repeating implementation details. Include delayed
responses and rapid typing when changing asynchronous search behavior. For CSS,
layout, stacking and mobile interaction bugs, use a real browser; Jest's jsdom
environment cannot verify painting or element overlap.

### Frontend unit tests and build

Use **Node.js 22** and **Yarn Classic 1.22.22**. Keep `yarn.lock` in sync with
intentional dependency changes; use frozen installs for verification.

```bash
(
  cd elastic-indexer-stack/frontend
  yarn install --frozen-lockfile --non-interactive
  CI=true yarn test:ci --runInBand
  yarn build
)
```

Jest enforces at least **90% statements, branches, functions and lines**. Do not
lower thresholds or exclude changed behavior to make tests pass. Coverage reports
are generated under `elastic-indexer-stack/frontend/coverage/`. The Docker build
also runs the full Jest suite and coverage gate before compiling the application;
it can be used when Node/Yarn are unavailable on the host.

### Built image, browser and embedding checks

The following reproduces the main CI checks locally with Docker and Python 3:

```bash
docker build --build-arg GIT_SHA="$(git rev-parse HEAD)" \
  --tag eqarchives-frontend:local elastic-indexer-stack/frontend
python3 -m venv .venv
.venv/bin/pip install -r scripts/browser-test-requirements.txt
.venv/bin/playwright install --with-deps chromium
EXPECTED_GIT_SHA="$(git rev-parse HEAD)" \
  BROWSER_TEST_PYTHON="$PWD/.venv/bin/python" \
  bash scripts/smoke-frontend.sh eqarchives-frontend:local
docker build --tag eqarchives-mcp:local elastic-indexer-stack/mcp
bash scripts/smoke-embeddings.sh eqarchives-frontend:local eqarchives-mcp:local
```

- [smoke-frontend.sh](scripts/smoke-frontend.sh) checks NGINX health, assets, build
  revision and credential isolation. It also runs `scripts/check-static-assets.py`
  against NGINX: gzip must preserve JS/CSS contents, fingerprinted assets must be
  immutable/cacheable, mutable pages must revalidate, and missing assets must be
  genuine 404s. Setting `BROWSER_TEST_PYTHON` also runs the
  [Chromium regressions](scripts/test-frontend-browser.py); CI always sets it.
  Without it, the script performs only the container smoke checks.
- Browser tests mock API responses and use disposable containers with dummy
  credentials. They cover sort-menu overlap with empty and floating date labels
  at 320, 390, 768 and 1280 px, plus sorting and date-picker interaction. They also
  verify lightweight result requests, reader navigation and the semantic
  search toggle/keyword-query behavior at mobile and desktop widths. Grouped-capture
  regressions cover duplicates spanning raw batches, unique results across pages,
  the all-captures toggle and exact dated previews. Extend
  this suite for browser-dependent bugs. Recently indexed site regressions cover
  the metadata-only `/sites` list, mobile tap targets and exact domain filters
  through semantic/quoted searches, sorting, pagination, readers and URL history.
  Explore regressions cover linked charts, phrase/cloud lists, date drafts,
  share/history state, delayed responses, mobile touch targets, cloud size
  separation/equal counts and search scopes.
  `PLAYWRIGHT_CHROMIUM_EXECUTABLE` can
  select an existing local Chromium binary; the default uses Playwright's install.
- [smoke-embeddings.sh](scripts/smoke-embeddings.sh) downloads the pinned model,
  verifies its hash and offline cache reuse, and runs the real server on CPU. It
  checks authentication, the NGINX proxy, 768-dimensional embeddings, oversized
  input rejection and recovery. No production credentials or GPU are needed.

For deployment-script or manifest changes, also run the workflow's validation:

```bash
bash -n scripts/deploy-frontend.sh scripts/smoke-frontend.sh scripts/smoke-embeddings.sh scripts/wait-http.sh
sh -n elastic-indexer-stack/k8s-manifests/embeddings/download-model.sh
sh -n elastic-indexer-stack/k8s-manifests/embeddings/start-server.sh
sh -n elastic-indexer-stack/k8s-manifests/embeddings/check-gpu.sh
python3 -m py_compile scripts/frontend-secrets.py scripts/check-embeddings.py scripts/test-frontend-browser.py
python3 scripts/test-http-readiness.py
kubectl kustomize elastic-indexer-stack/k8s-manifests >/dev/null
```

For documentation-only changes, check the diff, referenced paths, commands and
accuracy against the source files; application regression tests are unnecessary.
This does not itself exempt a normal PR from the required CI check.

### Indexing backend

Use Python **3.13**, Git and system `libmagic`, as specified in the indexer
Dockerfile. Legacy backend changes need their own pytest verification; the required
workflow does not deploy legacy workers or reindex Jobs. It does build/test the
separate curation image and manifest import entry point with Python 3.13 pytest.

```bash
(
  cd elastic-indexer-stack/indexer
  python3.13 -m venv .venv
  .venv/bin/pip install -r src/indexer/requirements-dev.txt
  PYTHONPATH=src .venv/bin/python -m indexer.chunking --download-tokenizer /tmp/nomic-tokenizer.json
  PYTHONPATH=src .venv/bin/python -m indexer.gemma --download-tokenizer /tmp/gemma-tokenizer.json
  GEMMA_TOKENIZER_PATH=/tmp/gemma-tokenizer.json NOMIC_TOKENIZER_PATH=/tmp/nomic-tokenizer.json PYTHONPATH=src .venv/bin/python -m pytest src/indexer/test
)
```

The backend dependency set includes substantial document and ML tooling. Keep
test work isolated from live ingestion. Both worker modes require configured
Elasticsearch, RabbitMQ, model endpoints and a shared archive checkout; see the
root README for environment variables and secret mounts.

Broad text repairs use the separate, resumable
[`reindex-text-job.yaml`](elastic-indexer-stack/indexer/k8s/reindex-text-job.yaml)
workflow in the [indexer Jobs guide](elastic-indexer-stack/indexer/k8s/README.md).
Preserve existing enrichment/OCR/provenance through partial, concurrent-safe
updates and retain version checkpoints. Use a dedicated source checkout, an
exact-ID probe and the pinned tokenizer before starting a broad run. Missing
sources must be counted without claiming extraction repaired. Nomic chunking
uses at most 480 tokens including document prefix/special tokens and roughly 48
tokens of overlap; retain complete source coverage and 768-dimensional vectors.
Budget unknown WordPiece spans conservatively: the local tokenizer's long-word
shortcut can undercount what the Nomic server processes. Retain completed version
checkpoints when repairing that counting bug. Resume failed broad runs in a new
Job after probing the previously failing exact IDs; preserve old Jobs and the
dedicated source checkout, and do not repeat source preparation.
The default Vulkan profile keeps Nomic requests serial with at most two chunks
per request, a 480-token chunk budget, 768-dimensional vectors and no intentional
sleep. The operator-selected CUDA alternative in `scripts/embedding-service`
uses the same pinned Q8_0 weights and alias, 64 slots with 512 tokens per input,
and a 512-token compute microbatch. `NOMIC_EMBEDDING_PROFILE=cuda` permits up to
64 inputs per request; the measured starting point is eight inputs, base64 wire
encoding and four workers. Production indexers use only the CUDA service;
Vulkan is reserved for live search and MCP. Handoffs remove inherited
`REINDEX_SECONDARY_*` settings and explicitly set both primary and embedding
URLs to CUDA. Curation imports and saved indexing Job templates also target CUDA.
Opt-in `REINDEX_PREPARATION_WORKERS=2` moves CPU extraction
and pinned-tokenizer chunking into spawned processes without inheriting network
clients. Their complete text/offsets are checked before the original embedding
and ES workers continue. Existing Jobs default to no preparation pool.
Preserve full source coverage, indexed response identity,
complete finite vectors and checkpoint versions. Reject incomplete batches before
saving a document. Keep real near-limit compatibility and oversize recovery checks.
The current model upgrade uses `reindex_embeddings.py`: EmbeddingGemma 2 Q8_0,
768 dimensions, Gemma's pinned tokenizer, 480-token source-preserving chunks and
48-token overlap. Use `task: search result | query: ` for queries and
`title: none | text: ` for documents. Live search/MCP stay on eqvm Vulkan;
all indexing uses RTX 3080 CUDA. Replace chunks, summary and OCR vectors atomically
before marking `embedding_model=embeddinggemma-2-768-v1`; semantic search must
filter that marker while lexical search covers the full corpus. Preserve old
extraction checkpoints, enrichment, source identity, dates and last_indexed. No
paid enrichment during this backfill. Do not reuse Nomic checkpoints as Gemma
completion. The Nomic repair entry point remains for historical recovery only.
Follow the embedding-service migration runbook for operator-authorized handoffs.
Gemma text uses `eq-passages-480-v12`: budget title/context with every 480-token
request, retain full reader text, verify source-bound selected-view coverage and
store view offsets/provenance separately. Unknown layouts retain content;
format-specific navigation/post/table handling needs regression evidence. Resume
requires both model and passage version; prioritize the unfinished model frontier
before older Gemma records. Never embed enrichment placeholder summaries.
Forum extraction uses structural phpBB/WWWThreads/Snitz/vBulletin signatures across hosts;
retain guest and damaged-header messages, quoted bodies and genuine data tables.
Known error/utility projections clear every semantic field atomically, retaining
reader/enrichment metadata. Keep table contexts meaningful, pack small prose
sections without crossing post/feed/quote boundaries, and preserve fenced code.
Validate against private live-capture samples plus unrelated layouts. Quality
counters in reindex logs count only acknowledged writes; they are diagnostics,
not a relevance score. Keep the independent image Job untouched in text handoffs.
vBulletin supports complete message-comment boundaries, per-post IDs and stripped
archive author/body pairs. Retain attachments and genuine tables/quotes; damaged
boundaries fall back without dropping messages. Upgrade older passage checkpoints.
Source decoding is shared by the crawler, imports and legacy indexer, without
hostname exceptions. Validate full bytes, support CP932/Shift-JIS, EUC-JP and
ISO-2022-JP using declaration/script evidence, and preserve unaffected encodings.
Bounded damaged Japanese sequences retain U+FFFD markers and a counted, hashed
decoding provenance; excessive damage fails visibly. Website extraction uses
`html-2026-11-05-script-evidence`; retain mail/newsgroup checkpoints. The Gemma
resume query also requires that checkpoint for textual websites. Reuse vectors
when the reader and source-bound passage plan are identical; repair corrupted
readers/vectors atomically and source-proven garbled titles only. Retain original
bytes, enrichment, dates and image Jobs; no paid enrichment for decoding repairs.
New format-specific exceptions require evidence of a major site or widely used
structured engine; prefer broadly applicable source/DOM rules for other content.

The forum policy retains source-bound vBulletin numbered/paired boundaries,
UBB.classic Author/Topic rows and Lithium message/user/date cells across hosts.
Retain damaged-header fallbacks and real quotes/tables/attachments. Allakhazam
entity/comment projections may specialize the major site; unrelated layouts keep
content. Deep reader formatting is flattened without losing visible source data.
Version upgrades may reuse v4/v5/v6/v7 vectors only after exact request metadata and
source/view checks, never from version or body hash alone.
Snitz composers retain their populated quote and every topic-review message,
including text outside damaged rows; an uncertain review cannot select only its
textarea. Legacy vBulletin source regions and archive pairs, ASP.NET Forums,
UltraBoard and phpBB/Lithium printer views use cooperating format roles across
hosts. Everlore pairs database comment headers/bodies without replacing entity
data. Uncertain layouts preserve content and authored code/tables/attachments.
Generated error layouts clear semantic fields independently of URL action;
authored troubleshooting remains searchable. Exact per-plan embedding inputs
may share inference only with their complete title/context/model request;
return every original offset and validate all vectors before saving anything.
The v10 policy retains Lithium printer-view plain/coloured author lines without
profile ranks, and excludes only proven empty moderation forms. Shared RSS/Atom
projection binds each entry's subject/author/date to all of its chunks, preserves
unique summaries/full content, quotes, real code and tables, and escapes literal
fence decorations before Markdown chunking. Damaged XML keeps content through a
conservative fallback; never fetch XML entities or remote entry content. Preserve
raw readers, discussion identity and v9 checkpoints during operator handoffs.
The v12 fifty-site audit adds shared Snowboards, phpBB 1, old UBB and Magelo forum
boundaries, retaining quotes, data tables and attachments. Ezboard ad-only rows
must not invalidate message recognition; vBulletin can use a proven post cell
when one closing comment is absent, never borrow a previous author's metadata.
Catalog specializations require captured structural evidence: EQInterface,
EQRankings, MaximumEQ, EQDiva, EQPrices, EQSpoilers, GuildMagic guild/server context,
Graffe spell IDs and EverQuest
Online item entries. Bind entity/section/column context to continuations without
merging distinct spell/item entries. Keep raw fields, zero values and unknown
templates; shared calendar/ad/navigation cleanup cannot remove authored content.
The read-only LAN `/chunking-audit` dashboard stores all fifty decisions and bounded
source-linked before/after receipts. Full source fixtures and plans remain outside
Git. Run `python3 -m unittest discover -s scripts/chunk-audit -v`; never label
sample diagnostics as a corpus-wide quality or retrieval relevance score.
Preserve v11 checkpoints; exact source/view/request proof is required for reuse.
The v11 policy adds evidence-gated EQ Traders recipes/vendor sections and Magelo
profile panels, carrying source identity and column labels into every relevant
chunk. Bind detached profile headers to real data; remove only proven controls
and empty panels. Keep unfamiliar layouts and populated biographies/zero values.
Generic scaffolding rules exclude inert calendars, login widgets, decoded hosting
ad fallbacks and complete parking/default/error templates, retaining events,
authored directories, code and populated source textareas. Attach post/feed
attribution to its own first body block without merging entries. Bold first-row
data alone is not evidence of table headers. Preserve v10 checkpoints; only exact
source/view/request matches may reuse vectors. Ordinary delivery does not change
the active broad reindex Job. Routine same-model policy updates run in bounded
curation maintenance slices after legacy broad writers finish, yielding to imports.
Model/image migrations retain operator handoffs; do not restart broad Jobs for
ordinary chunking delivery.
Unclosed empty table rows must unwrap rather than delete their populated
descendants. Every archive-status projection clears summary/OCR vectors along
with body vectors, regardless of its diagnostic reason.
The v9 projection repair adds source entity context, dated article boundaries,
conservative generic navigation/utility filtering and canonical HTML newlines.
Retain raw-reader hashes and decoder behavior. Older v8 requests may reuse vectors
only after the full source/view/request checks. The v8 repair requires varied script evidence for undeclared Japanese hints and
recognizes complete large phpBB SQL-error layouts; retain explicit Japanese,
mixed regions, authored troubleshooting and full readers. Byte/provenance-equal
regional reader checkpoints may advance without extracting them again.
Optional text-embedding cache state is shared with imports under the existing
Samsung-backed curation PVC's `indexing-state` directory (10001:10001, 2770;
database files 0660). Continuations add supplemental group 10001 and retain all
other source/cursor/recovery mounts. Cache identity includes the full request,
model weights/runtime pins and tokenizer. Defaults: 512 MiB database, 100,000
entries, one-day heat half-life, seven-day hard idle expiry. Keep indexed decaying
heat eviction, bounded cleanup/journal growth, independent vectors and complete
batch validation. Optional cache failures bypass reuse visibly; mandatory
diagnostic failures remain visible. Persist rejected IDs, hashes, safe reason
categories and retry/resolution history independently of cache eviction. Resolve
only after an acknowledged repair; retry metadata alone after external success.
Explicit failed-ID retries stay bounded and an empty list cannot start a broad
scan. Preserve missing-source visibility and the existing worker failure status.
Missing website text uses `index_websites.py`: explicit pinned host scope, remote-free
Git metadata, verified local blobs, persistent private SQLite cursors/gaps,
create-only writes and CUDA only. No paid enrichment or repeated source preparation.
One bad file advances into diagnostics while later files proceed; transient outages
resume safely, integrity/auth failures stay visible. Preserve old Jobs, image state,
enrichment and archive files. A remaining gap count is incomplete recovery.

The separate CUDA vision companion uses the pinned projector in `vision.env`,
one image slot and the same Gemma space. Keep text/live services independent.
Install additive image/passages mappings before querying new vector fields.
Raster imports/backfills call no paid enrichment. Backfills use local Git trees,
source-blob verification, create-only writes and persistent private SQLite cursors;
preserve prepared sources and all previous Jobs. Keep per-file diagnostic reasons
and image input bounds. Run embedding-service and legacy backend regressions.

Operator-requested reindex tuning uses a prepared, checkpoint-preserving handoff
as documented in the Jobs guide; ordinary curation delivery still leaves existing
indexing Jobs untouched.
Authorized infrastructure continuations use `scripts/reindex-recovery`: preserve
the failed Job, original image, dedicated sources and checkpoints; probe an exact
remaining ID before the broad continuation. Wait for dependencies and retry
temporary transport outages without counting reboot disruptions against the
Job's lifetime backoff limit. Content/integrity/authentication failures must stay
visible. Keep the recovery ConfigMap immutable and the local image pinned; never
rerun source preparation or change extraction/chunking versions to resume.

### Search relevance evaluation

The operator benchmark in `scripts/search-benchmark` uses the frontend's actual
request builder and grouping identity. Run `python3 -m unittest discover -s
scripts/search-benchmark -v` for changes; frontend contract tests also run in
the normal Jest gate. Keep credentials and generated sources/vectors/results
outside Git. Use bounded, short-lived snapshots and serial inference while
ingestion is active. Model judgments require explicit opt-in for scoring and
must not be described as human validation. Preserve the tune/validation split,
report corpus and pooling limits, and validate against the completed corpus
before changing production defaults. Collection cannot write archive documents
or mappings; paid Luna grading is a separate explicit command.

### Archive candidate crawler

The operator tool in `scripts/archive-crawler` uses Python 3.10+ and Ruby 3.0+.
Run `python3 -m unittest discover -s scripts/archive-crawler -v` and its
`verify_review_browser.py` using the browser-test environment for changes. CI
runs both. Keep state, captured sources, judgments, decisions and keys outside
both repositories. Discovery uses a remote-free Git reader and bounded cached
tree inventories; do not enable implicit promisor fetches on older Git or walk
the archive checkout. Production source discovery keeps per-host Git-tree cursors
in the main SQLite database, independently of capped pilot file inventories and
campaign snapshots. Rotate across known hosts, prefer curated seeds/early pages,
and advance past cached or unavailable sources. Empty slices must continue while
the frontier has work under the original deadline/spend cap. Preserve metadata,
source-byte/time and probe bounds, report unavailable sources separately from
exhaustion, and never let an old resumed campaign rewind shared cursors.
New discovery campaigns use `shared-graph-v1`: initialize fresh per-run ledgers,
copy only host/board identity metadata, and read the shared link graph in place.
Never back up the entire portal database into each campaign or copy all shared
links/scans during frontier advancement. Retain exact candidate evidence, local
source additions, exclusions, original budgets/deadlines and initialized legacy
runs. Stage-source scans are shared across new runs; missing shared storage must
fail visibly rather than create an empty graph. Curation's eqvm state is on
`/mnt/samsung/eqarchives-curation`, persistently bound to the existing PVC path;
k3s requires that mount at boot. Preserve paths, ownership and all private ledgers
during checksum-verified offline moves. Local-path PVC sizes are not quotas.
Wayback requests are serial through a persistent client,
using the persistent Net::HTTP approach from upstream PR #280: no fixed request
delay or bandwidth cap, with cumulative request/byte/time budgets and bounded
422/429 backoff.
Paid Luna grading is explicit and separately budgeted; model judgments are not
human approval. Preserve complete evidence, exact URL/date identity, source
hash checks and stale-decision rejection. The pilot stages bounded page samples,
exports explicit approved batches, and cannot publish archive Git changes or
write production index documents. The production service in
`elastic-indexer-stack/curation` adds persistent LAN review, explicit page/directory/
site-account scopes, bounded acquisition, automatic publication/indexing after capture approval and
create-only imports. Use its guide, Dockerfile pytest and `scripts/smoke-curation.sh`
browser/TCP checks for changes. Preserve the exact Linux
`websites/<host>/<timestamp>/<decoded path>` convention and refuse unsafe names or
identity/content collisions. Bind only the LAN IP, allow actual peers in
`192.168.0.0/16`, ignore forwarded headers and require literal Host/same-origin
JSON actions. There is no public route or authentication. Do not enable paid discovery on
deployment. Continuous scheduling requires the
operator's saved automatic-mode opt-in (default off, $2/day, minimum grade 2).
Persist revision-checked settings and browser-local unsaved drafts. Saved automatic
mode disables and greys the entire manual Discover pane, including its slider and
advanced controls; drafts and budget waits cannot re-enable it. Disabling the saved
mode restores manual controls and drafts, subject to worker availability. Keep one
shared private SQLite daily/monthly reservation ledger across portal grading and new
import Jobs; reserve atomically before calls, retain uncertain costs and count
corrections. Seed prior portal spend on initial configuration and daily-only
migrations; later settings changes use the initialized shared monthly ledger
without reopening historical campaigns. Use UTC
midnight resets, retain original operation limits/deadlines, and resume funded
work automatically. New manual and automatic approvals record $5/site enrichment.
Keep original policies explicit and immutable through failed-file lineage. The saved
shared per-site ceiling authorizes changes for legacy approvals too, counting all
settled charges, uncertain reservations and corrections in the original site ledger.
Day/month resets and retries never renew this allowance.
A daily-budget import pause must remain in Indexing with a current Job/manifest
bound diagnostic; create a new numbered continuation only when funded. Never
modify previous Jobs or infer indexing completion from a budget exit. New import
Jobs require a verified completion diagnostic. All approved imports that exceed
the Job time limit continue in a new numbered Job, including legacy/manual
approvals, retaining the original budget/cache and immutable Jobs/manifests. Never
renew a site allowance, change an immutable policy or override a saved failure diagnostic. Disabling automatic mode stops new
automatic discovery/approvals; approved work continues under the daily cap.
The operator-selected limits are $20/day and $500/calendar month, with $5/site; deployment
preserves saved settings and automatic-mode opt-in. Both limits include actual and
uncertain reservations across workers, UTC resets and prior current-month costs.
Approved enrichment has priority over discovery; deny grading before reservation
while heartbeat-backed import demand remains, and automatically resume original
operations without renewing their limits/deadlines. Per-site failures cannot keep
priority indefinitely. Do not change caps or enable while an older paid Job lacks
shared monthly accounting; changing the site ceiling also requires per-site accounting
capability in unfinished paid imports. Never alter existing paid Jobs to add it.
Automatic promotion requires current `eq-site-selection-v2` source-bound grading,
verified coverage and high confidence. The displayed 0–3 selection grade is capped
by separate EQ relevance and archival-substance grades and the operator's focus.
Titles, redirects, frame notices, parked/error pages and empty forms cannot count
as substantive evidence. Small guilds, concise character/item data, useful link
collections and community life can be valuable without a minimum word count.
Preserve board/shared-account boundaries and explicit human scope edits. Expand
a deep link only to a source-evidenced dedicated EQ homepage/subsite boundary;
never infer a mixed parent host's topic from an EQ child. Within an established
EQ boundary capture generously: all descendants, assets, dates, galleries, social
pages, lore and off-topic community discussion. Do not add per-page EQ keyword
filters. Optional context uses the same serial Wayback client and cumulative
budgets: at most two in-scope frame/entry targets and two homepage boundaries
(one source-linked ancestor plus the account root), at most two dated captures
per homepage. Keep complete grading text within 120,000 characters; missing parent
evidence keeps a narrower useful scope. Parent context outside the approved scope
must never leak into captured/indexed output. Version paid grades/caches; existing
legacy approvals remain verifiable with their known original grader signature.
Pending legacy grades need the new assessment before automatic approval, under
their original ledger/budget; preserve cached responses, manual decisions and all
active/completed capture/import/reindex Jobs.
Keep lower grades in Saved for later and exclude their identities from discovery.
Restore, Undo and manual scope edits must prevent automatic reapproval. Preserve
explicit human decisions. Exhausted link frontiers wait five minutes. Candidate recovery retains its explicit/owned rules. Approved publication transport
failures retry under the current manifest with durable five/fifteen-minute/hourly
backoff; preserve declines, scope edits and explicit retry pauses.
Approved captures independently retry recognized Wayback failures and worker
interruptions after 5 minutes, 15 minutes, then hourly, preserving checkpoints and
cumulative usage. Unknown errors and approval/scope/integrity failures require a
correction. Preserve explicit pauses and cancelled resumes while other sites advance.
Never let one failed site hold a worker queue, in either manual or automatic mode.
Each explicit Discover run targets
50 new sites meeting its saved minimum grade (default 2), continuing past low grades and unavailable samples, until the target,
one-hour deadline or $2 total Luna reservation cap is reached. Stop visibly when
links are exhausted; pause on transport/authentication failures. Keep serial
Wayback transport and cumulative limits, incremental source-bound results,
durable reservations and the original deadline on explicit resume. Old fixed
shortlists and one-site manual checks retain their original bounds.

Exclude already archived website/account scopes independently of capped page
inventories. Keep shared-host accounts distinct, preserve source URL identity,
and recognize bare/www plus literal `:80`/`:443` and legacy `_80`/`_443` archive
folders during duplicate checks. Keep nondefault ports separate and invalidate
cached coverage when alias semantics change. Block approval/publication when
coverage is unverified. Rechecks use bounded
local Git metadata and resume without automatic fetches or archive walks.
Coverage rechecks must expose incomplete results, their pause reason and saved
snapshot progress; a successful HTTP request is not verified coverage. Use the
latest site/account result ahead of older capped page inventory flags. Large host
trees remain within the cumulative metadata byte/time limits, without a smaller
per-tree ceiling that prevents resumable checks from starting. Custom
capture folders are absolute URL paths within the site's account; saving a new
scope invalidates the previous approval and must persist across reloads.

The mobile portal has four exclusive active stages: Candidates, Capture queue,
Capturing and Indexing. Production approval moves a candidate to
Capture queue. Indexed sites retire automatically to secondary History; declined,
dismissed and duplicate sites also stay there. Deferred sites live in Saved for
later. Restore can return only deferred/dismissed candidates to Candidates, without
approving or starting work. Stage counts and filtering use all review status metadata,
independently of capped batch inventories. Preserve existing operator API filters.
Confirmation and error notifications have an accessible 44px close button. Closing
a message never sends an action, undoes a decision or changes the selected route;
polling must not reopen a dismissed message. Preserve the separate Undo action.
The queue has no count cap; after a 60-second grace period the automatic worker claims
one oldest eligible site immediately before starting it. Keep later sites queued
and undoable until they start; the explicit operator API retains its five-site
batch limit and existing claimed work remains unchanged. Preserve typed transitions,
atomic Undo/claim locking and durable progress in the appropriate stage. A temporary
capture failure automatically rejoins the FIFO when its saved retry time is due;
unknown or integrity failures need explicit resume and never hold the capture queue. Explicit capture
resume joins the uncapped FIFO queue, even while another capture runs or automatic
mode is on. Retain the operation, batch, approval, sources and cumulative budgets;
claim only when the capture worker is free. Cancel queued resume restores the pause
until atomic claiming wins, and prevents automatic restart recovery from overriding
that cancellation. Existing multi-site batches resume together. Retain failed
preflight approvals as undoable entries with a decision-bound error; skip them
until the operator changes the approval. Show failures independently of capped
operation lists, and isolate import submission failures per site. Reconcile an
uncertain Kubernetes create before advancing; retain the foreground-import serialization guard.
Candidate gathering,
capture, and publication have independent serial workers under one process lease;
slow downloads, Git publication and waiting imports must not block the other
workers. Scope busy checks, claims and explicit resume to the relevant worker.
Share one persistent Wayback client across capture, discovery and evidence checks,
with fair turns per bounded command, no fixed request delay or bandwidth cap,
shared backoff and separate durable operation budgets. Apply this connection policy
to resumed portal work without rewriting saved source manifests or cumulative
limits. Waiting counts toward the original deadline; closing a session
must not close another worker’s connection. Keep serial publication with the
foreground-import serialization guard.
Run the sole import Job reconciler independently of publication under that same
lease, with a ten-second idle interval and publication-completion wake. Long
Git work must not delay completed Job reconciliation or approved imports. Include
this thread in health/shutdown/lease retention; never add a second submission loop.
Expose worker availability independently of capped operation lists, require all
workers and the shared transport for health and retain the lease until all stop.
SQLite BUSY/LOCKED failures retry worker turns without killing a lane. After
external work succeeds, retry only its metadata transaction, never downloads or
Git publication. Preflight/import reconciliation contention stays retryable rather
than marking a site's content failed. Preserve shutdown, leases and checkpoints;
other database failures remain visible. Test real writer contention and HTTP health.
Coverage refresh commits each conditional candidate update/event before the next
site check; never retain SQLite's writer across a warm whole-corpus pass. Indexing
retry runs source verification off the HTTP event loop and outside the write lock,
then rechecks Job/manifest/publication/state atomically. Busy admission returns a
sanitized 503 with Retry-After; stale decisions cannot advance or duplicate a Job.
Keep pipeline status as a compact collapsed disclosure on each stage and site
workspace, outside disabled manual discovery controls. Navigation to another
stage collapses it; polling preserves the user-opened state. Show worker/queue/
retry counts and stale connection status in its summary; expanded details retain
the full pipeline activity. Show current phases, capture progress from Queue,
automatic retry/reset countdowns, Job blockers and bounded recent outcomes from
SQLite metadata independently of list caps. Report stale or failed polling instead
of implying fresh progress; status reads never start work or lose browser drafts.
Portal list polls (`compact=1`) read saved SQLite metadata without archive
maintenance and prepare full review records only for the visible page. Keep
counts, filters and FIFO ordering complete and within one read snapshot so a
concurrent worker claim cannot invalidate sorting. Workers and action preflights
retain coverage validation; preserve the original operator API refresh behavior.
Deployment readiness uses the same compact Queue request; it must not run archive
maintenance or load complete capture manifests to verify a rollout.
Preserve operations,
approvals and progress across restart; approved capture transport interruptions
retry independently of automatic discovery mode. Manual discovery/publication
interruptions retain their explicit resume semantics.
Source reading and scope drafts must not prevent progress polling or lose edits.
Download completion creates an independent manifest per site and automatically
queues whole-site publication/indexing under the original capture approval.
There is no Review tab or second approval. Show the automatic flow and current
cumulative site enrichment ceiling before capture approval, including mobile quick actions.
Preflight original approval, current scope/hash, coverage, policy and every saved
source before atomically queuing publication once. Persist failures as
index_preflight_failed in Indexing, requiring explicit manifest-bound retry;
polling cannot retry or pay. Preserve dated source/asset browsing in Indexing and
History, plus metadata-only migration of unapproved mixed capture groups. Never
copy sources, replay approved work or reactivate declined/dismissed/indexed sites.
Legacy Review URLs alias to Indexing. Keep operator review APIs compatible;
explicit reconsider of a declined site resumes automatic preparation.
Keep publication status and explicit retry on the affected site. Preserve the
selected view/site across reloads and the open source page during status updates.
The header light/dark toggle follows the device initially and persists an explicit
browser choice. Theme changes never call an action API or disturb drafts/readers.
Apply the palette before first paint without relaxing the CSP. Check both themes,
text contrast and touch targets in real mobile and desktop Chromium.
Design for phones first: bottom stage navigation, compact site lists, focused site
workspaces and a reachable action bar. Confirmed capture approval returns to the
Candidates list, retaining search/pagination/position without an approval popup.
Undo approval lives directly on each Queue entry and its workspace until capture
starts, including after the grace period. Capture completion moves the site
into Indexing automatically.
Candidates support direct mobile swipe-right approval and swipe-left dismissal,
with equivalent accessible buttons, saved scope shown, and Undo. Preserve vertical
scrolling and tap-to-open; short, cancelled, reversed or stale gestures cannot
submit decisions. Keep coverage/grading checks and unsaved-scope blocking identical
to the workspace. Candidates default to minimum grade 2, sort descending by grade
before pagination, and retain lower grades behind the 0–3 slider. Keep ungraded
sites accessible through Needs grading and preserve unfiltered operator API calls.
Advanced grading accepts optional content criteria (at most 1,000 characters) for
new discovery/manual runs and explicit one-site regrades. Blank preserves the
legacy EQ-only signature. Scores require both EQ relevance and the requested
focus; show the criteria with the grade. Freeze criteria on queued/running runs
and resume with their saved criteria/deadline/budget. Changing criteria cannot
reset one-site budgets. Bind source signatures and immutable paid-response caches
to the criteria; retain earlier valid grades for reuse and reject stale merges.
Block capture approval while a site's check runs. Edits/polling never start paid
work. Browser-local discovery drafts must survive polling and reloads.
The one-site evidence/grading action is explicit, source-bound and capped at $2;
show sampling failures and phase progress. Reuse verified staged sources and retain
the same operation, transport/spend budgets and cached valid grade on retry. Recheck
coverage before spending and reject stale merges after source or human decisions
change. Dismiss all confirms and atomically moves every Candidate-stage row to
History, including filtered/paginated rows, without changing other stages. Bulk
Undo must reject intervening changes atomically. Neither filtering nor polling
can start paid work. Luna spend in the upper-right header totals UTC today/month from
the pilot, initialized run and enrichment ledgers, independently of list caps.
Once shared accounting is configured, report its seeded integer reservation ledger
through one bounded read-only snapshot; do not rescan per-site databases on each
poll or double-count their attempts. Legacy reporting remains for unconfigured
accounts. Broken shared accounting must stay visibly incomplete.
Keep known usage estimates separate from unresolved reservations; include rejected
and correction attempts without double-counting cached responses or retries.
Use bounded, cached, read-only metadata queries; never walk sources or archive Git.
Incomplete accounting must be visible rather than displayed as zero.
Failures cannot advance it or return to the list. Discover & grade is a primary action
on Candidates, with its explicit 50-site target, one-hour/$2-total limits and a
visible candidate-worker blocker. Capture, publication and indexing do not disable
discovery, manual submissions or evidence grading. Persist each completed candidate as it arrives and poll every five seconds
while visible, displaying qualified/checked counts, phase, time, spend and stop
reason. Polling may enable it but must never start a paid run. Paused discovery
resumes there within its original bounds. Manual URL submissions also live on
Candidates: unwrap Wayback links, preserve original page identity and submitted
provenance, run only that site's normal coverage/sampling/Luna workflow with an
explicit one-site/$2 maximum, and reuse existing website/account candidates or
pending operations without changing decisions or resetting budgets. Unverified
coverage pauses before spending; archived sites retire without sampling/grading.
Whole-site approval lives outside the
document reader. Use separate mobile page-list/reader screens with normal scrolling
and URL-backed page/version navigation. Desktop may show both labeled panels with
independent keyboard/button scrolling. Preserve Back navigation, page filters,
scope drafts and reading positions during polling and refresh. Exercise long lists,
source races, stage transitions, retries, retirement and touch layouts in Chromium.
Terminal indexing retries must bind the failed Job and published manifest,
create a new numbered Job, retain prior Jobs and reuse the original site budget.
Missing/null content types use the saved MIME fallback and normalized media type
through readers, enrichment and imports. Unexpected import exceptions must write
a safe current Job/manifest-bound failure diagnostic before exiting.
Archive paths collapse repeated separators as the Linux downloader filesystem
does, including embedded URLs in query filenames. Accept old immutable manifest
spellings without changing their hashes; use canonical paths for Git and document
IDs and reject canonical duplicates and file/directory collisions. Retain identical
existing regular files and their modes, including 100755; never accept symlinks or
overwrite different bytes as a mode correction.
The non-root curation UID must have a Unix account so OpenSSH can start; exercise
the real SSH configuration offline in the built-image smoke check.

Do not alter, suspend, delete or restart existing indexing Jobs or their source
checkouts during curation delivery. Its controller has Jobs get/list/create and read-only pod status list
permissions for owned import diagnostics, without pod mutation/log/exec access. Foreground imports wait only for other unfinished, unsuspended
foreground capture imports, including pending/retrying Jobs with active=0. Broad
reindexes and maintenance never block that slot. The publisher uses its own bare treeless repository
and dedicated archive deploy key, without an archive worktree or force push. Keep
private state on its PVC. Import Jobs mount approved sources read-only and a
separate enrichment subdirectory writable. They receive their dedicated
create-only ES account, existing Nomic key and only the paid Luna key item,
never the publication key. AI enrichment is enabled by default using the
existing text prompts/schema enums, with source-bound caching, conservative
reservations and a separate $2 cap per manually approved site (automatic approvals
retain their configured daily policy). Preserve full source,
model provenance and supported date evidence; capture dates are not publication
estimates. Enrichment failures leave the new document pending. Skip existing
IDs without paying for enrichment or overwriting their metadata.
Cache paid responses before validation, including rejected evidence, to prevent
repeated charges on retries. Date evidence may vary only in whitespace; retain
the matched verbatim source excerpt. Omit unsupported/invalid optional date claims
and their evidence, clearing an unsupported guessed date to null; never substitute
a capture timestamp. Keep source text, summary and tags intact, and persist a
versioned private validation receipt with omission counts/reasons. Strict wire
shapes, non-date metadata, refusals and resource bounds still require validation.
Prefer an already-paid response with more supported dates within the same bounded
correction chain, retaining its signature without another API call.
Invalid or incomplete received enrichment responses can use at most two separately
cached correction requests under the original site's budget. Keep rejected responses
immutable, retain correction provenance, reuse legacy paid caches, and count lost
correction responses against that durable limit. Refusals remain pending. Surface
safe predefined failure reasons through Job/manifest-bound diagnostic files; never
expose model output or let a previous attempt's status override the current Job.
Enrichment uses 32,768 output tokens for initial/correction requests; reserve the
full allowance before sending. Reuse exact source-bound 4K paid caches and retain
their correction identities. The operator-authorized 32K migration has one durable
single-use recovery slot only for a received old 4K `max_output_tokens` failure
after its correction chain is exhausted. Keep old costs/history and unknown
reservations; never replenish correction slots or unknown initial attempts across
limit/prompt versions. Refusals, other failures and 32K exhaustion stay bounded.

New approved captures cover all available dated versions from inclusive UTC
1999-01-01 through 2006-12-31. The 1999–2001 preference ranks discovery evidence
and equally graded candidates; it must not truncate an approved site's date
coverage. Newly claimed ordinary page/directory/site captures use the versioned
`complete-files-v1` engine: paginated, uncollapsed HTTP-200 CDX scope inventories
for every file type, including orphan URLs and saved grading samples. Preserve
exact URL/date identity across HTTP/HTTPS/www aliases within the approved account
path. Stream binary files and Git hashes; keep catalog/file checkpoints in SQLite
without growing per-file JSON rewrites or archive walks. Source-verified HTML/CSS
references can add exact supporting-file URLs, including external assets, without
crawling their hosts. Whole-board engines retain ownership checks and collect
verified supporting files afterward. Exclude case-insensitive hostname prefixes
`ad.` and `ads.` before new supporting-file queries and replays, including saved
pending/failed work on resume; do not match similar names or URL paths. Keep
intentional exclusions visible and separate from retryable failures. Preserve
already saved sources and immutable completed manifests, including their source
reference verification. Apply the same host filter to discovery/manual candidates.
A transport/storage/ownership bound keeps
new work paused in Capturing; only exhausted catalogs and pending records reach
Indexing, with missing/substituted replays explicitly listed as gaps. Individual
file HTTP 403/404/410 responses are durable gaps that do not block other files,
including board pages. Temporary file and exact supporting-file lookup failures
get three durable attempts across worker turns; process other URLs before
retrying, then advance the site to indexing with explicit gaps. Yield after
three consecutive temporary failures. Rate limits and primary scope inventory
failures retry periodically without being mislabeled as complete coverage.
Expose retry countdowns, Retry now and Pause retries; honour cancellation atomically. Completed ordinary sites and boards offer Retry failed
files: queue only missing versions/lookups, copy private checkpoint metadata,
reuse successful sources, revalidate ownership of retried board pages, keep usage cumulative and previous manifests immutable.
Undo remains available until claiming; stale manifests cannot retry. Published
sites may retry only their saved missing files under the identical approved scope
and hash-bound published predecessor. Retain existing Jobs and published manifests;
older import completion cannot retire newer acquisition. Retry manifests share
the original site's enrichment_budget_id, paid response cache and original daily or $2 total cap,
with diagnostics still bound to each current manifest/Job. Validate that lineage
before publication or any paid import. A pending published-file retry must finish
publication before another missing-file retry. Show these controls in Indexing
and History; no second approval is needed when retry finishes.
Explicit resume
may extend exhausted capture transport allowances without resetting cumulative
usage, paid limits or discovery deadlines. Retain all already claimed legacy work,
reviewed/published manifests and saved operator policies. Unapproved ordinary-site
legacy reviews offer explicit Regenerate full capture, returning to the unlimited
queue with the original scope, source reuse and cumulative transport usage. Retain
the old immutable manifest and create a separate full inventory on claim; a retained
source set never replaces the CDX scope inventory. Undo regeneration restores the
original capture status until claim, using the same atomic lock. Block individual, bulk and
legacy-API publication of these legacy subsets until regeneration completes. Never
automatically regenerate on deployment/polling or replay already approved work.
Mobile review separates
readable pages from supporting files with safe attachment downloads, 100-URL list
pagination, compact status polling and manifest-hash reuse. Publish all files in
one site commit; index readable HTML/plain text, retaining assets without Luna calls.
Sampling must distinguish archived redirects/errors from an empty calendar.
Verify an entry redirect's chain within the current scope/account, independently
list/capture its destination, and retain actual URL/date plus entry provenance.
A changed scope on explicit retry must update the run's eligibility checks without
resetting its operation, sources or budgets; reject approval of a page scope that
excludes its graded redirect source.
Replay comparisons must accept adding/removing the scheme's default port (HTTP
80, HTTPS 443), retain the CDX spelling and differing replay-original URL as
provenance, and continue rejecting other ports, source changes and exact-date
substitution. Capture cards and workspaces show known processed/remaining totals,
determinate progress and a recent-throughput ETA. Inventory totals may grow;
unknown totals and stalled estimates must remain explicit. Show the active phase,
current URL, query date range and update age on Capture cards as well as workspaces.
During archive listing, show catalog progress instead of a misleading 100% file
count; do not estimate the site's finish time from an incomplete inventory.
Use the uncapped capture worker snapshot for active card progress. Ezboard capture
prioritizes submitted and source-proven servers before speculative archive hosts,
retaining every catalog, date tier, source and continuation on resume.
Persist progress across
reloads, retain counts on pause, and recalculate ETA on resume without counting
paused time or reused sources. Polling cannot start or resume capture.

Ezboard uses the [custom capture engine](scripts/archive-crawler/EZBOARD.md).
Candidates represent a source-verified top-level board across numbered Ezboard
servers, never individual forum/message URLs. Preserve exact source URLs, query
strings and dates. Resolve ambiguous concatenated forum names using archived
navigation; do not infer ownership from prefixes alone. Archive coverage shares
bounded resumable Git metadata across boards and blocks unresolved forum-only
matches. Whole-board capture is an explicit scope, with paginated CDX prefixes,
durable budgets and source-verified publication. Keep existing approved scopes
and active work unchanged when consolidating unapproved legacy suggestions.
Operator captures never publish/index automatically. Run the crawler tests,
curation image tests and real browser checks when changing this flow.
Recognize Ezboard hostnames independently of historical ports when filtering
candidates. Profile/form links on port 8080 are not ordinary sites. Reject their
evidence/grading checks before downloading or paying, and retain approved scopes
and active checks when moving unapproved legacy rows to Saved for later.
Exclude the source-verified FreeServers `id=ezboard-r1` signup promotion, while
keeping hosted accounts and other redirect targets eligible. Retire unapproved
legacy adverts to History with a reason, without fabricating human decisions,
starting paid work or changing active/approved records. Candidate failures need
persistent card and workspace explanations: failure phase, plain-language cause,
the exact original URL when relevant, and a useful next action. Keep raw safe
diagnostics available separately; polling must update or clear stale errors
without starting retries or disturbing an open source reader.

SitePowerUp uses the [BoardID capture engine](scripts/archive-crawler/SITEPOWERUP.md).
Group candidates by numeric BoardID across host aliases and query order/case;
retain exact original source URLs, queries and dates. `Action=Reply` is a readable
message view despite its name. Whole-board capture includes indexes, messages and
pagination, excludes other BoardIDs and posting/admin actions, and verifies saved
source ownership. Legacy `_and_` filenames inform board coverage only; never
reconstruct exact source identity from them. Share bounded, resumable Git metadata
across boards without page/blob reads or implicit fetches. Consolidate unapproved
legacy suggestions without copying sources or altering approved scopes/active
checks. Keep separate board reviews, cumulative transport limits and explicit
partial-coverage reporting. Run both crawler and curation/browser regressions.

### Public MCP service

The service in `elastic-indexer-stack/mcp` uses Python 3.13 in production and
supports 3.10+ for local development. Install its hash-locked
`requirements-dev.txt` and run `python -m pytest` from that directory. Tests
include HTTP initialization, tool schemas, search/fetch behavior, source fidelity,
embedding fallback, cache behavior, credential isolation and request limits.
Branch coverage is enabled with a 90% combined coverage threshold; do not lower it.
The MCP Docker build runs these tests before creating the production runtime.

Pass an MCP image as the second argument to `scripts/smoke-embeddings.sh` to
exercise the real MCP container with CPU Nomic and the isolated Elasticsearch
fixture and the production ingress rules in an isolated Traefik container; CI always
does this. The routing test uses `scripts/deployment-test-requirements.txt`,
`DEPLOYMENT_TEST_PYTHON` and the pinned test image in `scripts/routing-test.env`.
Keep MCP's explicit ingress priority: the root `PathPrefix` rule is longer than the
exact MCP rule and otherwise wins Traefik's default ordering. After deployment run
`python3 scripts/check-mcp.py https://search.eqarchives.org/mcp`.

The same container integration runs `scripts/test-frontend-rollout.py`: it executes
the frontend manifest's actual preStop command, delays the ingress endpoint update,
and verifies continuous requests through real Traefik/NGINX containers. Preserve
the frontend's 10-second preStop and 75-second total termination grace; the image
uses SIGQUIT to drain requests after endpoint propagation. This regression fails
when the old frontend stops immediately.

Frontend container readiness uses `scripts/wait-http.sh` so transient startup
connection resets are retried. `scripts/test-http-readiness.py` verifies recovery
after a reset and rejection of a service that stays unhealthy.

Preserve `search(query)` and `fetch(id)` compatibility, output schemas, read-only
annotations and matching structured/JSON text results. Document IDs are opaque
exact ES IDs, never filesystem paths or URLs. Do not silently truncate source
text or substitute generated summaries; keep OCR/date estimates labelled.
Never expose upstream credentials, errors, vectors or arbitrary Elasticsearch DSL.
Keep retrieval/cache limits appropriate for the shared single-slot model server.
The additive `search_archive` tool provides keyword research with exact source
filters, independent inclusive UTC capture/publication/indexing windows, sorting and a bounded
1,000-result window (1–50 results per page). Preserve the explicit lower-bound
total and limit-reached signals. Pagination uses the live index; do not claim
snapshot consistency. Keep replica preference and legacy-record tie ordering.
`list_sources` discovers domain/mailing-list/file-type/MIME/theme/tag values with
composite paging (1–100 values), using ES's returned after-key, plus fixed archive
collections. Model tags/themes are labelled. Literal URL/account scopes are parsed
into escaped ID queries, never fetched; Ezboard membership requires archived board
links. `list_sites` is a metadata-only newest-domain directory with a second bounded
aggregation for exact per-shard counts. Keep historical first-arrival dates unknown;
new imports record conservative forward provenance without updating old records.
`list_captures` returns exact dated IDs, and optional search grouping uses exact
page or source-verified discussion identity; disclose the 1,000-record scan bound.
Availability filters mean indexed field presence; fetch reports nonempty text/OCR.
These research tools share retrieval limits and must not call the embedding service. Cover filtering, invalid
dates, pagination boundaries and old search/fetch compatibility with regressions.
The user chose direct public MCP access with a top-right MCP icon and a connection
screen for ChatGPT developer mode and Claude. Do not prepare or submit an official
OpenAI directory listing unless requested later. Only claim account-level
ChatGPT/Claude or Deep Research validation after actually performing it.

## Application constraints

- React uses Elastic Search UI and Material UI. Shared appearance is defined in
  [ArchiveTheme.css](elastic-indexer-stack/frontend/src/views/ArchiveTheme.css)
  and [theme.js](elastic-indexer-stack/frontend/src/theme.js); keep the archive
  palette and mobile interactions consistent.
- Search responses must not replace newer text the user is typing. Preserve the
  existing asynchronous input regression coverage when changing search controls.
- Applied date ranges belong to Search UI's filter/URL state. New searches must
  pass `shouldClearFilters: false`; do not keep a separate global date registry.
  Preserve draft picker edits across results and equivalent filter objects, and
  restore applied values on reload/history navigation. Define the date control's
  `withSearch` wrapper at module scope so loading updates cannot remount it.
  Apply replaces a range
  atomically; Clear removes it from both requests and the URL. Date selections
  include both UTC calendar days. Keep keyword matching required when filters
  are present and apply those filters to every semantic kNN branch. Browser
  regressions cover both date fields, delayed results, mobile filters, successive
  searches, reload/back/forward and timezones on either side of UTC.
- Result cards request metadata and a short escaped `text_full` highlight; full
  text, OCR bodies, nested chunks and vectors are excluded from result `_source`.
  Keep nested KNN ranking but do not return unused `inner_hits`. **Read document**
  is the sole full-text action on result cards; do not restore the redundant
  **Preview Full Text** button or mount preview dialogs in result actions. The
  reader occupies the full-width primary action row on phones. The separate
  capture-history previews use a size-one `ids` query through the existing search
  proxy and return only `text_full`. Prefer the actual Elasticsearch `_meta.id`;
  preserve loading, empty/error/retry states and cancellation.
- Repeated website captures group by exact original page by default. Preserve
  query strings, path case and protocol differences; never group by title or merge
  unrelated messages/attachments. `GroupedSearch.js` scans metadata in 50-record
  batches within the existing 1,000-capture window and caches one active search.
  Group representatives retain the selected search/sort order. Keep groups unique
  across pages, invalidate on filters/sort/semantic settings, and stop superseded
  scans. Counts/facets describe captures, not unique groups; use Previous/Next and
  label browsing limits. This is live-index pagination, not a snapshot.
  `CaptureService.js` loads dated history separately, including versions outside
  current filters, with escaped exact-ID patterns and exact URL validation of
  legacy candidates. Keep metadata/full text separate, exact preview IDs, retries,
  cancellation, and the 1,000-record history bound. No index migration is needed.
- Source-verified forum discussions may opt into a separate search grouping via
  `discussion.version=forum-discussions-v1`. Require structural platform evidence
  and source-bound topic identity; never infer membership from titles. Preserve
  exact `CaptureIdentity` for history/comparisons, protocols and query strings.
  Lithium print/reply membership needs locally verified RSS evidence; conflicting
  memberships stay separate. Show distinct matching excerpts in increments of
  three with exact reader IDs. Discussion readers use a page-specific capture
  date selector and collapsed Archive details, without synthesizing whole-thread
  snapshots. Keep the source catalog private and shared through indexing-state.
  Existing records use the bounded, resumable `index_discussions.py` operator:
  pinned prepared Git sources, explicit hosts, learn/annotate cursors, source
  hashes and optimistic discussion-only updates. No inference, paid enrichment,
  source preparation or running-Job handoff is needed for that metadata backfill.
- Dedicated readers use `/document?id=<exact ES ID>`; comparisons add
  `compare=<second ID>`, with optional `find` and `part=ocr`. Route readers before
  mounting the search provider so they do not launch searches or embeddings.
  Preserve exact `_meta.id` links, the completed `resultSearchTerm` context, and
  the legacy filtered-search links. Reader retrieval whitelists source/OCR and
  provenance through the existing proxy; keep the old lightweight preview
  contract separate. Aborted or superseded requests must not replace current
  content. Keep full source downloads, manual clipboard fallback, safe Markdown
  links and explicit OCR/estimated-date labels. Never auto-load source images or
  substitute generated summaries. Relative Wayback links must retain the capture.
  Comparisons require the same exact original-page identity, preserve whitespace
  and line endings, label additions/removals, and distinguish missing source from
  deleted text. Keep asynchronous bounded diffs (1M combined characters, 20K
  lines, 1s, 2K edits), complete-reader/download fallbacks, 500K-character Markdown
  fallback and the 1K highlight limit explicit; never silently truncate text.
  Reader browser regressions cover 320/390/1280 px, exact links, query highlights,
  source text/downloads, citations, dated comparisons, swap and direct reload.
- Keep embedding eligibility shared between the input and query builder via
  `QueryPolicy.js`: blank/operator queries, disabled semantic search, missing
  vector fields and service cooldown must not fetch unused embeddings. Preserve
  abort behavior for obsolete drafts, the response-body deadline and the bounded
  50-entry browser cache; cancellation must not mark the service unavailable.
- NGINX compresses JS/CSS/JSON/text/SVG responses. Only successful content-hashed
  `/static/` assets get a one-year immutable cache policy; HTML, guides and
  unversioned assets revalidate. Do not cache API responses or missing assets as
  immutable, or return the SPA shell for missing static assets.
- The status bar exposes the deployed Git SHA. The Docker `GIT_SHA` argument
  becomes `REACT_APP_GIT_SHA`; retain the display and bundle smoke check.
- [engine.json](elastic-indexer-stack/frontend/src/config/engine.json) and all
  `REACT_APP_*` values are public browser configuration. They must contain no
  upstream credentials. The development server alone does not provide API proxies.
- Browser APIs stay on the same origin: `/elasticsearch/eq-archive/_search`,
  `/elasticsearch/eq-archive/_count`, `/openai/v1/embeddings`, and the two
  `/api/explore/` routes. NGINX adds search/embedding authorization headers from
  runtime secret files; Explore uses the MCP service's read-only ES client.
- `/explore` links date/theme/site/source-phrase selections in URL state. Preserve
  explicit date drafts, stale-response cancellation, retry isolation, visible
  tagging/sample coverage, capture versus estimated-date semantics and mobile
  44px controls. Search links retain the website collection and every constraint.
  `/api/explore/overview` and `/api/explore/phrases` are exact public routes to the
  read-only MCP service. Keep the fixed query bounds, serial analytics execution,
  shared request limit, ten-minute/64-entry summary cache and safe errors. Phrase
  samples are bounded to 100 captures/12,000 characters each, deduplicate original
  pages and repeated text, and expose sampling/vocabulary limits. Retain exact
  sampled page counts when ranking phrases or scaling clouds. The bundled English
  frequency reference filters generic standalone words; retain its attribution,
  offline loading, capped rarity bonus and shared vocabulary bound. Common words
  remain eligible inside specific source phrases; long menu runs cannot be their
  only evidence. Ranking never changes page counts or minimum support. Equal counts use
  equal sizes; disclose relative scaling and keep zero-based bars in List. Never walk
  archive Git, write index documents or call AI for exploration. Test routes with
  `scripts/check-explore.py` in container integration and deployment; preserve
  the MCP tool contract and existing indexing Jobs.
- Nomic queries use the `search_query:` prefix, while the embedding cache is
  keyed by the original query. Preserve the configured model alias and
  **768-dimensional** vector compatibility with the indexed data. Changing the
  embedding model is not just an interchangeable frontend setting.

## CI/CD behavior

The active delivery workflow is
[elastic-indexer-stack-cicd.yml](.github/workflows/elastic-indexer-stack-cicd.yml).
It runs for PRs targeting `master`, pushes to `master`, and manual dispatches.
There is no release-please workflow or release/tag prerequisite.

The required build job runs on GitHub-hosted `ubuntu-24.04`. It validates scripts
and Kustomize, builds the frontend with Jest/coverage, runs container and browser
checks, builds/tests MCP with coverage, and exercises both containers with the real
Nomic service on CPU. It also builds/tests the Python 3.13 curation/import image,
runs its real browser and TCP peer/spoofing checks, and validates its separate
Kustomization. PRs get no production secrets
or self-hosted execution. Keep this required check present on every normal PR;
path-based workflow skipping can leave required checks pending.

Only `master` publishes `dbsanfte/frontend:<full-git-sha>` and the MCP artifact
`dbsanfte/frontend:mcp-<full-git-sha>` plus the curation/import artifact
`dbsanfte/frontend:curation-<full-git-sha>` to Docker Hub and deploys their independent
immutable image digests. A rerun reuses an already published image
for that SHA and repeats smoke checks. Base images, third-party actions and the
embedding runtime/model are pinned; update those pins deliberately.

Deployment uses the self-hosted runner `eqvm`, with labels
`self-hosted`, `Linux`, `X64`, `eqvm`. The runner is a trusted VM administrator;
do not route untrusted PR code onto it. It uses the local kubeconfig rather than
a cluster-admin credential stored in GitHub. Deployments are serialized in the
`eqarchives-frontend-production` concurrency group and are not canceled midway.
A queued run checks that its SHA still matches `master` before touching the cluster.

The deploy script validates rendered resources, reconciles secrets, brings up
and verifies Nomic/GPU service, then rolls the frontend and MCP service. It checks
HTTPS, search, document count, embeddings and a vector-only search through the
production proxy, followed by public MCP discovery/search/fetch checks.
It then deploys curation, verifies its LAN queue/build and rejection of non-LAN
forwarded-header spoofs, and guards unfinished Job UIDs/spec hashes across the
entire deployment.
Repeated deployment of the same image/configuration/secrets must leave Deployment
generations, revisions and pod UIDs unchanged. Do not add timestamp annotations
or routine `rollout restart` calls. Configuration hashes and credential checksums
trigger updates when their contents actually change.

## Production infrastructure

The VM **eqvm runs single-node k3s directly**. Earlier references to k3d do not
describe this deployment. The usual checkout on this VM is
`/home/dbsanfte/eq-archives-software`. Use the kubeconfig at
`/etc/rancher/k3s/k3s.yaml`; passwordless `sudo -n kubectl` is available. Docker is
available for builds and isolated checks. Production resources are in namespace
**`eqarchives-es`**.

| Component | Production configuration |
| --- | --- |
| Frontend | Deployment/Service `search-eqarchives`, four replicas, NGINX port 80, `maxUnavailable: 0`, `maxSurge: 1` |
| Public routing | Traefik Ingress `search-eqarchives-root`, HTTPS `search.eqarchives.org`; `search-beta.eqarchives.org` redirects to the primary site |
| TLS | cert-manager, existing `letsencrypt-prod` ClusterIssuer; certificate Secrets remain cluster-managed |
| Elasticsearch | Existing service `elasticsearch.eqarchives-es.svc.cluster.local:9200`, index `eq-archive`; managed separately from frontend CI |
| Embeddings | Deployment/Service `nomic-embeddings`, one replica, internal endpoint `http://nomic-embeddings.eqarchives-es.svc.cluster.local:8080`, zero-unavailable rolling updates |
| MCP connector | Deployment/Service `eqarchives-mcp`, one replica, port 8080, exact public `/mcp` HTTPS ingress, zero-unavailable rolling updates; uses the existing read-only ES/model credentials |
| Curation | Deployment `eqarchives-curation`, one replica/Recreate, LAN-only `192.168.68.53:8090`, no Service/Ingress/auth; separate 25 GiB staging PVC and controlled import Jobs |
| Model cache | PVC `nomic-embedding-models`, 1 GiB on existing `local-path` storage; reproducible model cache, not archive storage |
| GPU access | DaemonSet `eqarchives-vulkan-device-plugin`, AMD Radeon Vulkan device `/dev/dri/renderD128`, supplemental render GID 109 |

Nomic Embed v1.5 **Q8_0** runs in llama.cpp under alias
`text-embedding-nomic-embed-text-v1.5@q8_0`, with mean pooling, normalized vectors,
a 1,024-token aggregate context, two 512-token slots and eight host threads. All 13 layers
are offloaded through Vulkan. The device plugin exposes two shared
`eqarchives.org/igpu` allocations so an old and replacement pod can coexist; these
are not separate physical GPUs. Neither inference nor the device plugin requires
privileged mode. The pinned runtime rejects oversized inputs with HTTP 500 but
remains usable afterward; capacity changes need explicit testing.

The root Kustomization manages frontend, MCP and embedding resources only.
The separate `elastic-indexer-stack/curation/k8s` Kustomization manages curation
without applying backend references or changing existing ingestion Jobs.
[00-elasticsearch.yaml](elastic-indexer-stack/k8s-manifests/00-elasticsearch.yaml)
and [docker-compose.yml](elastic-indexer-stack/docker-compose.yml) are backend
references with environment-specific settings/placeholders, not a deployment
recipe for this CI workflow. Elasticsearch data, RabbitMQ, archive ingestion and
enrichment services have separate lifecycles. Do not apply the whole manifests
directory or scale/reinitialize those services as part of a frontend change.

## Secrets

Never commit secrets, kubeconfigs, runtime secret manifests, real `.env` files or
generated NGINX configuration. Avoid printing credentials in tool output, shell
tracing, process arguments, workflow logs, artifacts or PR text. Kubernetes Secret
data is only base64-encoded, not safe to publish. Use placeholders in examples.

| Repository Actions secret | Purpose |
| --- | --- |
| `DOCKERHUB_USERNAME` | Docker Hub account for the frontend image |
| `DOCKERHUB_TOKEN` | Registry push/pull token |
| `FRONTEND_ES_USERNAME` | Dedicated read-only Elasticsearch account |
| `FRONTEND_ES_PASSWORD` | Password for that account |
| `FRONTEND_OPENAI_API_KEY` | Shared NGINX/Nomic embedding API key |
| `ARCHIVE_CRAWLER_OPENAI_API_KEY` | Dedicated paid Luna key, only in curation |
| `ARCHIVE_PUBLISH_SSH_KEY` | Dedicated write deploy key scoped to dbsanfte/eq-archives |

The deployment pipes these into `search-eqarchives-secrets` and
`dockerhub-pull-secret` using server-side apply, avoiding secret-bearing files and
last-applied annotations. NGINX reads `es_readonly_username`,
`es_readonly_password` and `openai_api_key` under `/run/secrets`. Nomic receives only
the shared API key. The current frontend account is `frontend-cicd` with role
`frontend-search`, restricted to reading `eq-archive`.

Use `gh secret set NAME --repo dbsanfte/eq-archives-software` with hidden input or
trusted stdin. Coordinate credential changes between upstream services, Actions
secrets and runtime consumers so healthy replicas remain available. Old history
contains a revoked legacy read-only password; do not restore historical browser
credentials or reuse revoked values. The deployment guide records that rotation.

## Operations and completion

For read-only inspection on eqvm:

```bash
eqarchives_kubectl=(sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml)
"${eqarchives_kubectl[@]}" get nodes
"${eqarchives_kubectl[@]}" -n eqarchives-es get deployments,daemonsets,pods,services,pvc,ingresses
"${eqarchives_kubectl[@]}" -n eqarchives-es rollout status deployment/search-eqarchives --timeout=300s
"${eqarchives_kubectl[@]}" -n eqarchives-es rollout status deployment/nomic-embeddings --timeout=900s
"${eqarchives_kubectl[@]}" -n eqarchives-es logs deployment/nomic-embeddings -c download-model --tail=40
curl --fail --silent --show-error https://search.eqarchives.org/healthz
```

To redeploy the current `master` through the normal pipeline, when requested:

```bash
gh workflow run elastic-indexer-stack-cicd.yml \
  --repo dbsanfte/eq-archives-software --ref master
```

For an authorized manual deployment, load the five secret environment variables
from a trusted source and run `scripts/deploy-frontend.sh` with two known immutable
`dbsanfte/frontend@sha256:...` images: frontend first, MCP second. Use this script instead of applying the raw
manifests: their `deploy-via-ci` images are intentional placeholders. Full manual
deployment and idempotence procedures are in the deployment guide.

An emergency `kubectl -n eqarchives-es rollout undo deployment/search-eqarchives`
(using the same sudo/kubeconfig as above) restores the preceding pod template,
provided its credentials still work. It does not undo secrets or ingress changes.
For Nomic, undo `deployment/nomic-embeddings` and coordinate any shared-key change;
retain the cache PVC and device plugin. Follow operational recovery with a tested
revert through `master` so desired state matches production.

Legacy manual workflows are not routine diagnostics:
[diagnose-eqvm.yml](.github/workflows/diagnose-eqvm.yml) powers off a VirtualBox VM
and reinstalls VirtualBox, and [reboot-eqvm.yml](.github/workflows/reboot-eqvm.yml)
reboots the host. [diagnose-eqarchives-vm.yml](.github/workflows/diagnose-eqarchives-vm.yml)
targets the older `eqarchives-vm` runner label. Do not dispatch these during normal
development or service checks.

After a deployment, verify the workflow result, ready replicas, the status-bar
build SHA, and the changed behavior in a real browser. Include mobile verification
for responsive UI work. Report what changed, tests actually run, the PR/commit,
deployment status and any remaining limitation. Documentation-only work with an
explicit skip-CI request should finish without launching a deployment.


Routine same-model chunk updates use `curation/policy_maintenance.py` and
`policy_scheduler.py`: one 2,000-record/ten-minute slice, CUDA only, no Luna or
source preparation, cooperative yield between batches, 15-minute hard deadline.
Wait for legacy broad writers to finish naturally; leave their specs/pods/sources
untouched. Scope durable cursors/diagnostics and the least-privileged read/index
account to the physical ES index plus model/policy/extraction checkpoints. Use
exact bounded remote-free source reads and optimistic atomic updates; never
create/delete documents or downgrade newer policies. Preserve readers, enrichment,
dates and image workflows; reuse vectors only with exact source/view/request
proof. Quarantine per-record errors and repeated hard worker interruptions while
later IDs proceed; known transport pauses do not consume that allowance. Resolve
diagnostics only after acknowledged or fully verified external repairs. Use small
atomic control hints, never readonly portal SQLite WAL reads. Keep per-slice
progress/issue receipts bounded and visible in the collapsed pipeline pane; only
new disposable maintenance Jobs have a TTL. Preserve prior full/index Jobs.

Publisher Git ownership uses `publication_git.py`: an exclusive OS lease, no
archive traversal, fixed lock allowlist and live-process checks before private
quarantine of abandoned locks. Never remove a live/unsafe lock or a general glob
of lock files. Bound and reap the entire Git/SSH/pack process group on timeout;
retain fast-forward-only pushes, treeless/no-op fetches and marker recovery.
Head fetches also require `--refetch` so locally authored remote heads are present
in a bounded promisor pack; trace a repeated/uncertain successful publication and
reject archive-wide `--exclude-promisor-objects` connectivity walks.
Hash verified source files with `hash-object --stdin-paths --no-filters` in
512-path batches, using Git C quoting and checking every returned object ID/count.
Avoid per-file Git startups, archive traversal or buffering source bytes in Python;
retain the single site commit/push, ownership lease and immutable approval marker.
`publication_recovery.py` retries only the latest still-approved manifest and
shares transport backoff across the backlog. Permanent credentials/integrity
failures and explicit retry pauses stay visible for correction while other work
advances. Pipeline status must expose publication backlog/errors and the last
completed import; deployment health alone does not verify successful ingestion.

Curation imports use a 2 GiB limit (512 MiB request); exercise the actual pinned
Gemma tokenizer/planner in a fresh container at that budget in smoke-curation.
The old 512 MiB limit cannot load it. Permit read-only pod status listing for owned
import OOM diagnostics, without logs/exec/mutations. Only a failed old 512 MiB
profile auto-migrates to a new numbered current-profile import; preserve the old
Job, approvals, manifests, paid cache and budget. Current-profile OOMs stay visible
for correction and cannot hold other sites. Keep full reindex Jobs untouched.

Luna operation/site caps count verified actual costs plus only unresolved
reservations, under the same atomic admission lock and nanodollar rounding as
the shared UTC limits. Keep rejected requests, corrections and uncertain costs
in their original ledgers; never reset budgets on retry. Source-bound enrichment
dates are unique calendar days with one or more verbatim excerpts per day; accept
long news indexes up to the explicit 1,024-item metadata bounds. Normalize only
derived metadata, retaining distinct proof and immutable paid responses/signatures
so previously rejected but valid cached corrections can recover without new calls.
