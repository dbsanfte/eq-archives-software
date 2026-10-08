# Intranet curation and targeted indexing

Production review: **http://192.168.50.100:8090/** on eqvm's existing k3s cluster.
No authentication is required. The service binds only the node's LAN address,
accepts actual TCP peers in `192.168.0.0/16`, ignores forwarded-IP headers and
checks literal Host/same-origin action headers. There is no Service, NodePort or
public Ingress. Do not put a proxy that hides peer addresses in front of it.

The standard [CI/CD workflow](../../.github/workflows/elastic-indexer-stack-cicd.yml)
tests and publishes `dbsanfte/frontend:curation-<full-sha>`, then deploys its
immutable digest with [deploy-curation.sh](../../scripts/deploy-curation.sh).
The same image contains [index_captures.py](../indexer/src/index_captures.py).
Legacy finder/worker/broad reindex Jobs retain their separate lifecycles.

## Review flow

1. Review Luna's grade, reason, verbatim evidence and complete extracted source.
   Archived scripts, HTML and images never execute in this screen.
2. Choose scope and **Approve site for capture**. This queues the site automatically,
   removes it from **Candidates**, and moves it into **Capture queue**.
   The screen returns to the Candidates list with its search and position retained,
   plus a confirmation and **Undo approval** action so you can keep reviewing sites.
   The queue has no item-count cap; its 50-row pages are pagination, not a limit.
   New approvals have a 60-second grace period. **Undo approval** returns the
   site to Candidates and retains its sources and grade. Undo remains available
   until the worker atomically claims the site, even after the grace period.
   Deep links default to their containing
   directory and descendants; extensionless paths use that directory itself.
   **Linked page only** and **Whole site / shared account** are alternatives.
   **Custom folder and below** accepts an absolute URL folder path such as
   `/eq/research/`, with the host supplied by the candidate. Save the path before
   approving. It may select a different folder from the sampled page; reviewed
   links seed the traversal, and only in-scope pages enter the downloaded batch.
   Shared hosts retain their account boundaries. A scope change clears an
   earlier approval and changes its manifest hash.
   Unidentified shared-host accounts remain exact-page scopes, including roots.
3. The worker automatically processes the oldest eligible approvals in serial
   batches of up to five sites. This is a per-batch resource bound, not a queue
   size limit. Existing approvals receive a one-time grace period on migration;
   restarting does not reset it. A paused capture holds the queue until explicitly
   resumed, retaining its budgets. Items move into **Capturing** when claimed.
   The worker
   spiders HTML links within the scope, including its entry directory, through
   the pinned public Wayback Machine Downloader and serial persistent client.
4. Completed items move into **Review capture**, newest first. Open a site to browse
   its captured pages and dated Wayback links, including
   complete extracted source and multiple versions of the same page.
   **Approve site & index** approves every captured page within that
   site's chosen scope. This second approval binds to its complete manifest and
   actual source hashes. Import includes AI enrichment by default, with a separate
   $2 maximum shown for that site before approval. **Decline indexing** keeps its
   files in staging without publication or indexing; **Reconsider indexing**
   returns a declined site to review. Decisions are independent for each site.
   Publication can be queued during another capture; the serial worker publishes
   it before claiming another capture batch, so a large capture queue cannot
   prevent publication of already reviewed files.
5. Publication makes one fast-forward archive commit per approved site, including
   `crawl-manifests/<batch-id>.json`. Import waits for every other unfinished,
   unsuspended Job in `eqarchives-es`, including pending/retrying Jobs with
   `active=0`. The controller cannot patch, suspend or delete Jobs.

The portal has five active stages: **Candidates**, **Capture queue**, **Capturing**,
**Review capture**, and **Indexing**. Each candidate belongs to exactly one stage,
using its durable review status where available. Indexed sites retire automatically
to **History** and leave every active count; declined, dismissed, already archived,
and duplicate sites also stay in History. Deferred candidates are in **Saved for
later**. These two secondary lists are available from More. Restoring a deferred
or dismissed candidate returns it to Candidates without approving or starting work.
The portal includes lower-grade candidates after higher-grade ones, so they can
receive an explicit decision. Stage searches and 50-row pagination do not cap the
approval queue. Counts describe the entire stage, including rows outside a search.

On phones, bottom navigation opens compact stage lists. A site opens a focused
workspace showing its evidence/scope, queue position, capture progress, captured
subset, or publication/indexing status as appropriate. Capture approval returns to
Candidates; publication/indexing approval follows the site into Indexing. Undo
remains available until the atomic worker claim. A failed action cannot advance
the site or return to the list. Background updates refresh counts
without navigating away from another selected site. Next site and the stage list
support reviewing multiple sites. Scope edits require Save before approval.

The captured-page browser and complete document reader are separate mobile screens
with normal vertical scrolling. The searchable page list preserves its position
when returning from a document. The reader has dated Wayback links, capture-version
selection and thumb-reachable Previous/Pages/Next controls. On desktop, labeled
page/document panels appear side by side with independent scroll controls. Whole-site
approval appears on the site's decision screen, outside the reader. URL navigation
retains the stage, site, selected page and version across reloads; browser Back,
status polling and manual refresh preserve page filters, scope drafts and reading
positions. Sources are plain extracted text; archived HTML/scripts/images never run.

Capture progress refreshes every five seconds and shows actual worker-batch file,
byte and URL counts, with the current site named. Counts are labeled as batch
progress rather than attributed to another site. Capture errors stay in Capturing
with an explicit Resume; queued sites explain why they wait. Indexing separates
publication, waiting for existing Jobs, and AI enrichment/import, with no invented
percentage. Publication and indexing failures remain in Indexing with a contextual
Retry action. Technical Job details expand separately and remain open across polls.
Discover & grade is a primary action at the top of Candidates, with the explicit
50-candidate/$2 limit beside it. A visible explanation names any capture, discovery
or publication occupying the shared worker; polling makes the button available
when that work finishes, without starting a run. Paused discovery can be resumed
there within its original limits and budget. More contains recent operations,
capture limits and secondary history views. There are no scheduled paid runs.

Add a site, also in Candidates, accepts an original HTTP(S) URL or a Wayback
replay/calendar link. Bare addresses use HTTP; original protocol, path case,
escaping and query order are preserved. Wayback links identify the original page,
with the submitted link retained as provenance; sampling still prefers 1999–2001,
then 2002–2007. Submitting explicitly permits checking **one site, up to $2**.
The single worker checks archive/account coverage, takes the usual bounded samples
and runs the normal Luna grader. It does not spider out to other sites. Results
join Candidates with the ordinary evidence, sources, grade and capture-scope
review. Missing sources or invalid grades remain unapproved.

`POST /api/submit-site` requires `{"url":"https://example.org/eq/","max_usd":2}`.
It returns an operation ID, or the existing candidate ID for that website/account.
Repeated pending submissions reuse the same operation and its original budget;
existing decisions and captured sources are preserved. Already archived sites
retire to History without sampling or paid grading. Unverified coverage pauses
the operation before acquisition or spending, with explicit Resume site check in
Candidates. The form preserves edits while polling or submitting; completed checks
link to their site. The action never approves capture, publication or indexing.

Typed candidate transitions remain centralized in
`scripts/archive-crawler/capture_flow.py`; queue claiming and Undo use the same
SQLite write lock. `portal.py` projects those states and site review status into
exclusive presentation stages without replaying approved work or moving sources.
The stage API reads all review status metadata, independently of the recent-batch
list limit. `GET /api/candidate?id=…` returns the selected site's review, capture
operation and queue context, even when it is outside the current list page.
Legacy queue filters remain available to existing operator clients.

Capture batches are acquisition records. Each completed site receives an
independent review manifest referencing the existing staged files, so pages from
different sites or shared-host accounts never share a review decision. Existing
unapproved mixed batches are split idempotently on startup, without copying or
downloading files. Original capture manifests and hashes remain as provenance.
Previously approved publication/import records retain their original identities.
The legacy file-subset API remains available for single-site operator requests;
the review screen and site-decision API always approve the complete captured site.

[Capture limits](captures.py): five sites, 20 additional URL attempts per site,
100 files, 1 MiB per response, 64 MiB source/transport budget, 500 HTTP requests
including retries and 1,800 seconds per batch. Requests are serial, at least
three seconds apart, throttled to 128 KiB/s, with bounded 422/429/server-error
backoff. Each exact URL first tries inclusive UTC 1999–2001, then 2002–2007 if
no exact first-tier capture is listed. Initial samples can retain two versions;
traversal adds the oldest returned version per URL. CDX listings are bounded
and can be incomplete. These are HTML subsets, excluding images/assets and
current live pages; they are not complete mirrors. Reaching a batch budget
stages its valid subset for review with an explicit coverage note. Other
interrupted operations need an explicit resume within their original budgets.

## Archive convention and source fidelity

Files match the existing Linux downloader's all-timestamps convention:

```text
websites/guild.example/20000101000000/index.html
websites/guild.example/20000201000000/eq/news.html
websites/guild.example/20000301000000/forum.php?board=1&start=2
websites/guild.example/20000401000000/eq/index.html
```

The last path represents an extensionless/directory `/eq` capture. Filenames
use upstream CGI decoding, including `+` and percent decoding. The manifest
preserves the exact original URL, actual timestamp, raw SHA-256 and staging
location. Spidered filename collisions are excluded with review notes. Unsafe
names, conflicting initial samples and existing different archive bytes pause
publication. Do not rename files to
hashes or silently overwrite a legacy capture. Indexed URLs come from the
manifest instead of guessing protocol/path/query identity from filenames.

The initial pilot seeds 50 candidates, 35 judgments and 64 listed captures once,
preserving raw bytes and grading signatures. No decisions are automatically
approved. **Discover & grade · 50 sites · $2 cap** explicitly starts another run.
Each requested run owns its own durable cap; there are no scheduled paid runs
or grading calls on deployment. Reservations precede calls and survive ambiguous
failures. Valid unchanged judgments are reused on resume; unverifiable or
oversized sources remain unjudged. Model judgments are not human validation.

Discovery reuses cached local Git trees/link evidence, scans bounded verified
staging sources with grades 2–3 for outward links, and excludes known site/account
scopes. It probes selected local blobs through a remote-free reader without
implicit fetches. It never clones, pulls, enumerates a disk checkout or commits
either archive checkout.

Website/account duplicate checks use the Git host list and targeted tree
metadata independently of the capped page inventory. Known ordinary hosts are
excluded even when their linked page is absent or unenumerated. Shared hosts
are checked by account, preserving unrelated accounts. Discovery treats `www`,
protocol and default-port variants as site aliases; source URLs, capture identity
and published paths remain exact. No page blobs or checkout walks are needed.

The queue rechecks old candidates, retains their sources/judgments/decision
history and moves confirmed duplicates to **already archived**. They remain
visible under **History** but cannot be approved, captured or published.
Unverified account coverage also blocks approval. **Recheck archive coverage**
continues from the saved metadata position with fresh bounded probes. Checks use
at most 4,096 new trees per candidate, 16,384 cached trees and 32 MiB/15 seconds per
audit; unavailable partial-clone metadata is never fetched automatically. Approval,
capture execution and publication all enforce these checks.
Large shared-host timestamp trees use that same cumulative byte budget; there is
no smaller per-tree limit that can permanently stall a recheck. Partial results
report why the check stopped and how many snapshots were checked on the current
host. **Continue coverage check** resumes a bounded scan; missing local metadata
or a tree larger than the total budget is shown as requiring operator attention.
The API returns the resulting state and coverage evidence, and the UI explicitly
keeps approval blocked for incomplete checks. A completed site/account check takes
precedence over the original capped page inventory. Confirmed duplicates move to
History with a link to the existing archive path.

## Publication, indexing and secrets

`ARCHIVE_PUBLISH_SSH_KEY` is an Actions secret containing a dedicated write deploy
key scoped to `dbsanfte/eq-archives`. [github-known-hosts](github-known-hosts) pins
GitHub's Ed25519 host key. The publisher owns a separate bare treeless repository
on the PVC, explicitly fetches only needed ancestor objects through a remote-free
reader, disables automatic maintenance and never creates a worktree/index or
forces a push. Tests inspect its packs to ensure unrelated blobs and subtrees
are not downloaded. Retries verify the remote
marker and source blob IDs. A recovered commit denotes a verified snapshot
containing the batch and may be newer than its original publication commit.

`ARCHIVE_CRAWLER_OPENAI_API_KEY` is the separate paid Actions key. Both are
projected into `eqarchives-curation-secrets`; neither reaches the browser or
image. Import receives only the paid `luna_api_key` item, never the publication
key. It uses the existing Nomic key and a separate ES
user/role `eqarchives-capture-import`, restricted to `read` and `create_doc` on
`eq-archive`, from `eqarchives-capture-indexer-secrets`. The trusted deployment
runner reads the existing elastic password through stdin to provision this
account and four additive provenance keyword mappings. It does not change
existing accounts or Jobs. Secret apply uses a private pipe/server-side apply,
with no secret-bearing files or last-applied annotations.

The controller disables automatic service-account mounting and explicitly
projects its rotating token, CA and namespace into `/run/kubernetes`. This stays
beside the read-only `/run/secrets` credential volume: `/var/run` aliases `/run`,
so mounting a token beneath `/var/run/secrets` would prevent container startup.
Import Jobs have no Kubernetes service-account token. CI checks the configured
token location and starts the built container with both read-only volumes.

Import verifies the entire manifest/file set before any ES requests. It reuses
the website Markdown extractor and checksum-pinned WordPiece tokenizer, embeds
complete source serially with `search_document:`, at most 480 tokens including
prefix/special tokens and roughly 48 tokens overlap, and requires finite,
nonzero 768-dimensional vectors. IDs are exact `websites/...` paths. Existing
IDs/concurrent create conflicts are skipped, preserving enrichment, OCR and
provenance. By default, each new document also receives source-bound Luna
summary, content flavour, tags and supported date estimates using the existing
archive text prompts/schema enums. It stores the actual model and enrichment
signature, and embeds its short summary with Nomic. The complete original body
is retained separately; model estimates never replace capture dates. Date
extraction receives no synthetic capture header and requires source evidence.
Evidence matching allows only whitespace changes (wrapped lines and NBSPs),
then retains the original verbatim excerpt. Sources exceeding the 900 KB
serialized enrichment bound remain pending rather than being truncated.
Date prompts require one contiguous excerpt: a table's date cell must not be
joined to its column heading. A received response that fails field, completeness
or source validation gets at most two correction requests with the complete
source and specific validation failure. Corrections pass the same strict
validator; an unsupported quote is never accepted by relaxing the check.
An explicit model refusal remains pending without correction requests.

Each approved site has a separate cumulative $2 enrichment cap.
Reservations use conservative long-context rates and precede calls; ambiguous
failures retain reservations. Paid responses, including rejected evidence, are
cached by source/prompt/model signature in a writable `enrichment/` subdirectory
on the PVC. Existing results from the previous prompt remain reusable. Corrections
have separate numbered cache entries linked to their original response, retaining
all rejected evidence and the final matched verbatim excerpts. The original paid
response is never overwritten. Each correction slot can spend only once, including
an ambiguous/lost response, and the two-slot limit survives pod and Job retries.
An initial request with no received response has at most two reserved attempts.
Every reservation counts against the same site's $2 cap; retries neither reset
that cap nor silently create an unenriched document. Exhausted corrections require
operator attention rather than another paid loop.
The approved
sources/manifests remain read-only in import Jobs. Completed results are reused
on retry without paying again for a received response, while existing document
IDs incur no model calls. Failed enrichment or an exhausted budget leaves that
document pending instead of silently creating an unenriched entry. There is no
index/mapping creation, RabbitMQ or broad scan.
Kubernetes retries twice. A terminal failure shows **Retry indexing** on the
affected site's review. It verifies the saved publication approval and all source
hashes, then queues a new numbered import Job under the original site budget.
Duplicate/stale retry requests are rejected. The controller retains the failed
Job, waits for other unfinished Jobs and never republishes or overwrites indexed
entries. The selected source page stays open during retries and status updates.
New import Jobs write small diagnostic files beside their enrichment cache. These
bind to the exact Job name and approved manifest hash; the controller displays the
matching failure on the site's Indexing screen. Only predefined error messages
reach the portal/logs, never model output, source excerpts or upstream credentials.
Diagnostics do not grant the controller any additional Kubernetes permissions or
allow an older attempt's failure to replace a newer attempt's status.

## Storage, deployment and checks

The `eqarchives-curation` local-path PVC stores SQLite/WAL state, captures,
bounded campaign caches, manifests and publisher objects. Its request is 25 GiB;
local-path requests are not filesystem quotas. Back it up separately from the
public archive using SQLite's backup API plus manifest-listed captures. Preserve
publication/indexing state on restore and keep private state/keys outside Git.

One replica uses `Recreate` and an exclusive worker lease. A one-time init copies
only the pilot database and listed captures from a read-only mount, excluding
its tree reader, screenshots and orphan downloads. Both archive checkouts,
existing ingestion Jobs and the model service stay untouched. Rollback applies
the preceding compatible immutable curation digest and retains the PVC. After
site decisions have been recorded, use a worker that supports the site-review
states; finish pending publications before downgrading to the earlier batch-review
worker. Unchanged deployment
inputs preserve the Deployment generation/pod UID.

```bash
docker build -f elastic-indexer-stack/curation/Dockerfile \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" -t eqarchives-curation:local .
BROWSER_TEST_PYTHON=/path/to/playwright-venv/bin/python \
  bash scripts/smoke-curation.sh eqarchives-curation:local
kubectl kustomize elastic-indexer-stack/curation/k8s >/dev/null
```

The image build runs Python 3.13 API/state/scope/source/campaign/publication/import
pytest, including an over-50-item queue and Undo/worker claim races. The runtime
defines a Unix account for UID/GID 10001: OpenSSH requires the passwd entry even
when the private key and destination are supplied explicitly. The container
smoke check exercises SSH configuration offline as that non-root user. CI also uses
real Chromium at 320/390/430/768/1280 px, with touch emulation on phones, long
page lists and complete document screens, desktop keyboard/button scrolling,
Back navigation and source position across status polling, delayed source/search
responses, scope drafts, exclusive stage transitions, Undo during delayed refreshes
and worker claims, draining pagination, durable whole-site decisions, exact dated
versions, publication/indexing retry, deferred/dismissed restoration and automatic
retirement from active views
in isolated fixtures with no
paid calls or real archive writes. Real TCP tests reject non-LAN peers and
forwarded-header spoofs. Deployment checks the live queue/build and hashes
existing unfinished Job specs before/after; it never saves their credential-
bearing JSON to disk.
