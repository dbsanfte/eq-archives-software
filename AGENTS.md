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
bash scripts/smoke-embeddings.sh eqarchives-frontend:local
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
  this suite for browser-dependent bugs. `PLAYWRIGHT_CHROMIUM_EXECUTABLE` can
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
  NOMIC_TOKENIZER_PATH=/tmp/nomic-tokenizer.json PYTHONPATH=src .venv/bin/python -m pytest src/indexer/test
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
the archive checkout. Wayback requests are serial through a persistent client,
with cumulative request/byte/time budgets, throttling and bounded 422/429 backoff.
Paid Luna grading is explicit and separately budgeted; model judgments are not
human approval. Preserve complete evidence, exact URL/date identity, source
hash checks and stale-decision rejection. The pilot stages bounded page samples,
exports explicit approved batches, and cannot publish archive Git changes or
write production index documents. The production service in
`elastic-indexer-stack/curation` adds persistent LAN review, explicit page/directory/
site-account scopes, bounded acquisition, whole-site publication/indexing approval and
create-only imports. Use its guide, Dockerfile pytest and `scripts/smoke-curation.sh`
browser/TCP checks for changes. Preserve the exact Linux
`websites/<host>/<timestamp>/<decoded path>` convention and refuse unsafe names or
identity/content collisions. Bind only the LAN IP, allow actual peers in
`192.168.0.0/16`, ignore forwarded headers and require literal Host/same-origin
JSON actions. There is no public route or authentication. Do not run paid discovery
on deployment or schedule it without a request. Each explicit Discover run targets
50 new sites meeting its saved minimum grade (default 2), continuing past low grades and unavailable samples, until the target,
one-hour deadline or $2 total Luna reservation cap is reached. Stop visibly when
links are exhausted; pause on transport/authentication failures. Keep serial
Wayback transport and cumulative limits, incremental source-bound results,
durable reservations and the original deadline on explicit resume. Old fixed
shortlists and one-site manual checks retain their original bounds.

Exclude already archived website/account scopes independently of capped page
inventories. Keep shared-host accounts distinct, preserve source URL identity,
and block approval/publication when coverage is unverified. Rechecks use bounded
local Git metadata and resume without automatic fetches or archive walks.
Coverage rechecks must expose incomplete results, their pause reason and saved
snapshot progress; a successful HTTP request is not verified coverage. Use the
latest site/account result ahead of older capped page inventory flags. Large host
trees remain within the cumulative metadata byte/time limits, without a smaller
per-tree ceiling that prevents resumable checks from starting. Custom
capture folders are absolute URL paths within the site's account; saving a new
scope invalidates the previous approval and must persist across reloads.

The mobile portal has five exclusive active stages: Candidates, Capture queue,
Capturing, Review capture and Indexing. Production approval moves a candidate to
Capture queue. Indexed sites retire automatically to secondary History; declined,
dismissed and duplicate sites also stay there. Deferred sites live in Saved for
later. Restore can return only deferred/dismissed candidates to Candidates, without
approving or starting work. Stage counts and filtering use all review status metadata,
independently of capped batch inventories. Preserve existing operator API filters. The
queue has no count cap; after a 60-second grace period the automatic worker claims
one oldest eligible site immediately before starting it. Keep later sites queued
and undoable until they start; the explicit operator API retains its five-site
batch limit and existing claimed work remains unchanged. Preserve typed transitions,
atomic Undo/claim locking and durable progress in the appropriate stage. A paused
capture requires explicit resume and holds the queue. Source
reading and scope drafts must not prevent progress polling or lose edits. Download
completion creates an independent review for each site. Review and approve all
captured pages within one site's chosen scope at a time, with dated Wayback links
and complete sources. Preserve independent approve/decline/reconsider decisions,
manifest-bound approvals and metadata-only migration of unapproved mixed capture
groups; never copy sources or replay already approved publication/import work.
Review's Approve all and Dismiss all confirm every Review-stage site, including
filtered and paginated rows. Preflight all manifests, coverage and sources before
atomically queuing separate whole-site publications; display the combined $2/site
enrichment cap. Never partially approve a stale snapshot. Dismiss all retains
sources in History with atomic Undo that rejects intervening decisions.
Keep publication status and explicit retry on the affected site. Preserve the
selected view/site across reloads and the open source page during status updates.
Design for phones first: bottom stage navigation, compact site lists, focused site
workspaces and a reachable action bar. Confirmed capture approval returns to the
Candidates list, retaining search/pagination/position without an approval popup.
Undo approval lives directly on each Queue entry and its workspace until capture
starts, including after the grace period. Publication/indexing approval follows
the site into Indexing.
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
Keep known usage estimates separate from unresolved reservations; include rejected
and correction attempts without double-counting cached responses or retries.
Use bounded, cached, read-only metadata queries; never walk sources or archive Git.
Incomplete accounting must be visible rather than displayed as zero.
Failures cannot advance it or return to the list. Discover & grade is a primary action
on Candidates, with its explicit 50-site target, one-hour/$2-total limits and a
visible shared-worker blocker. Persist each completed candidate as it arrives and poll every five seconds
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
The non-root curation UID must have a Unix account so OpenSSH can start; exercise
the real SSH configuration offline in the built-image smoke check.

Do not alter, suspend, delete or restart existing indexing Jobs or their source
checkouts during curation delivery. Its controller has only Jobs get/list/create
permissions and waits for other unfinished, unsuspended Jobs, including pending/
retrying Jobs with active=0. The publisher uses its own bare treeless repository
and dedicated archive deploy key, without an archive worktree or force push. Keep
private state on its PVC. Import Jobs mount approved sources read-only and a
separate enrichment subdirectory writable. They receive their dedicated
create-only ES account, existing Nomic key and only the paid Luna key item,
never the publication key. AI enrichment is enabled by default using the
existing text prompts/schema enums, with source-bound caching, conservative
reservations and a separate $2 cap per approved site. Preserve full source,
model provenance and supported date evidence; capture dates are not publication
estimates. Enrichment failures leave the new document pending. Skip existing
IDs without paying for enrichment or overwriting their metadata.
Cache paid responses before validation, including rejected evidence, to prevent
repeated charges on retries. Date evidence may vary only in whitespace; retain
the matched verbatim source excerpt and reject all other content differences.
Invalid or incomplete received enrichment responses can use at most two separately
cached correction requests under the original site's budget. Keep rejected responses
immutable, retain correction provenance, reuse legacy paid caches, and count lost
correction responses against that durable limit. Refusals remain pending. Surface
safe predefined failure reasons through Job/manifest-bound diagnostic files; never
expose model output or let a previous attempt's status override the current Job.

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
verified supporting files afterward. A transport/storage/ownership bound keeps
new work paused in Capturing; only exhausted catalogs and pending records reach
Review, with missing/substituted replays explicitly listed as gaps. Explicit resume
may extend exhausted capture transport allowances without resetting cumulative
usage, paid limits or discovery deadlines. Retain all already claimed legacy work,
reviewed/published manifests and saved operator policies. Mobile review separates
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
unknown totals and stalled estimates must remain explicit. Persist progress across
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
filters, inclusive UTC capture/estimated-publication dates, sorting and a bounded
1,000-result window (1–50 results per page). Preserve the explicit lower-bound
total and limit-reached signals. Pagination uses the live index; do not claim
snapshot consistency. Keep replica preference and legacy-record tie ordering.
`list_sources` discovers domain/mailing-list/file-type values with composite
aggregation paging (1–100 values), using ES's returned after-key. Both tools share
retrieval limits and must not call the embedding service. Cover filtering, invalid
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
  `/elasticsearch/eq-archive/_count`, and `/openai/v1/embeddings`. NGINX adds the
  upstream authorization headers from runtime secret files.
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
| Curation | Deployment `eqarchives-curation`, one replica/Recreate, LAN-only `192.168.50.100:8090`, no Service/Ingress/auth; separate 25 GiB staging PVC and controlled import Jobs |
| Model cache | PVC `nomic-embedding-models`, 1 GiB on existing `local-path` storage; reproducible model cache, not archive storage |
| GPU access | DaemonSet `eqarchives-vulkan-device-plugin`, AMD Radeon Vulkan device `/dev/dri/renderD128`, supplemental render GID 109 |

Nomic Embed v1.5 **Q8_0** runs in llama.cpp under alias
`text-embedding-nomic-embed-text-v1.5@q8_0`, with mean pooling, normalized vectors,
512-token context/batches, one request slot and eight host threads. All 13 layers
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
