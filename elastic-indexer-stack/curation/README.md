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

Use the light/dark toggle in the header to change the entire portal, including
source readers and dialogs. It initially follows the device's appearance and
remembers an explicit choice in this browser across visits.

1. Review Luna's grade, reason, verbatim evidence and complete extracted source.
   Archived scripts, HTML and images never execute in this screen.
2. Choose scope and **Approve site for capture**. This queues the site automatically,
   removes it from **Candidates**, and moves it into **Capture queue**.
   The screen returns to the Candidates list with its search and position retained,
   without a capture approval popup. **Undo approval** lives directly on each
   **Capture queue** entry and in its workspace.
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
3. The worker automatically claims the oldest eligible approval just before
   starting that site's capture. Only one site is claimed at a time, keeping later
   sites in Queue and available to Undo. The explicit operator batch API still
   accepts up to five sites; existing claimed batches keep their saved work.
   Existing approvals receive a one-time grace period on migration;
   restarting does not reset it. A paused capture holds the queue until explicitly
   resumed, retaining its budgets. Items move into **Capturing** when claimed.
   The worker inventories the entire approved scope through paginated Wayback
   CDX listings, then downloads every listed successful file version. It includes
   orphan pages, images, CSS, scripts and downloads, using the pinned downloader
   with a serial persistent client. Supporting files referenced by HTML/CSS are
   checked at their exact URLs, including on external asset hosts.
   Individual file replays returning HTTP 403/404/410, and those responses from
   exact supporting-file lookups, become visible coverage gaps while the rest
   continues. Failure to inventory the approved site, service/rate-limit errors,
   and transport/storage limits still pause acquisition with its checkpoint.
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
   For a completed ordinary site with gaps, **Retry failed files** queues only
   missing versions and supporting-file lookups, retaining every successful file.
   **Undo file retry** returns its unchanged review until the worker starts.
   Retries copy private SQLite catalog metadata, retain cumulative transport
   usage, and produce a new review without publishing or indexing anything.
   Prior manifests stay immutable; successful catalogs are not requested again.
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
The minimum-grade slider under Discover more sites defaults to **2** and remembers
your choice in this browser. Grades sort highest first before pagination; lower
grades remain available by lowering the slider. **Needs grading** opens sites
without readable samples or a validated grade. Stage searches, grade filters and
50-row pagination do not cap the approval queue. Stage counts include hidden rows.
The operator API remains unfiltered unless `min_grade=0..3` or `needs_grade=1` is
supplied to `GET /api/queue?filter=candidates`.

**Review capture** has **Approve all** and **Dismiss all** controls outside the
reader. Their confirmation includes every awaiting-review site, even when search
or pagination hides it, and an expandable site list. Approval validates all
sources/manifests/coverage before atomically queuing independent whole-site
publications and moving to Indexing; the dialog shows the combined AI enrichment
maximum ($2 per site). A stale or invalid site blocks the entire approval.
Dismissal declines indexing, retains sources in History, and offers atomic Undo
unless a later decision changed a site. No other stage is affected.

**Dismiss all candidates** confirms the full remaining count, including sites hidden
by grade/search filters and other pages, then moves those sites to History. It
does not affect any other stage or start work. The confirmation offers **Undo
dismiss all**. Both actions are atomic and reject a changed review snapshot;
individual History restores remain available if a later decision prevents bulk Undo.

On phones, bottom navigation opens compact stage lists. A site opens a focused
workspace showing its evidence/scope, queue position, capture progress, captured
subset, or publication/indexing status as appropriate. Capture approval returns to
Candidates; publication/indexing approval follows the site into Indexing. Undo
remains available until the atomic worker claim. A failed action cannot advance
the site or return to the list. Background updates refresh counts
without navigating away from another selected site. Next site and the stage list
support reviewing multiple sites. Scope edits require Save before approval.

Candidates also support decisions directly in the list: swipe right on a phone to
approve the saved capture scope, or left to dismiss to History. The direction and
release threshold are shown while dragging. Vertical scrolling, short/reversed
swipes and cancelled touches make no decision. Each card shows its saved scope and
has equivalent Approve capture/Dismiss buttons for keyboard and pointer use.
Coverage, grading, source identity and unsaved-scope checks still apply. Confirmed
decisions remove the card while keeping Candidates open. Capture approval has no
popup; Undo stays on the Queue entry until capture starts, even beyond 60 seconds.
Dismissal retains its Undo confirmation. Tapping the card still opens its
evidence and scope workspace. Polling cancels an outdated gesture before it can
submit a stale decision.

**Advanced · Luna grading criteria** under Discover more sites adds an optional
content focus, such as “Cleric class sites and healing guides” or “guild communities.”
It applies to new discovery runs and **Add & grade site**, and is retained in this
browser. Blank uses the original general EQ grading. Scores remain 0–3; a high
score requires both EQ relevance and a match to the requested focus, using only
the supplied source evidence. The minimum-grade filter applies to that score.
Criteria do not alter crawling scope, dates, discovery limits or indexing enrichment.

Each run saves its criteria; paused runs resume with those criteria and their
original deadline and budget, regardless of later edits to Advanced. Repeated
manual submissions reuse the existing site/check without changing its criteria
or decision. A candidate workspace shows **Graded for** and its own Advanced
control to explicitly **Regrade with these criteria**. One-site regrades reuse the
same operation and original total budget (at most $2, including all earlier
attempts); they never replenish it. Criteria are limited to 1,000 characters and
become part of the prompt/source signature. Paid responses, including invalid
responses, and earlier valid assessments are cached by that signature. Switching
back to cached criteria reuses the assessment without another API request.
Approval waits while the site's grading check runs. Source changes and concurrent
human decisions still reject stale results. Editing criteria or polling starts
no paid work; custom criteria affect only an explicitly requested grade.

The upper-right Luna counter shows estimated USD spend today and this month using
UTC calendar boundaries. Expand it for the scope of accounting and unresolved
request reservations. Totals include the seeded pilot, every initialized discovery
or manual grading run, and all site enrichment attempts, including rejected and
corrected responses. Received usage counts once; cached responses and Job retries
add no cost unless they make a new paid request. Requests with no recorded usage
retain their conservative reservation separately, rather than being reported as
known spend. These are this portal's usage estimates, not an account billing total.
The display refreshes with status polling, with up to ten seconds of accounting
cache. Only shallow staging ledger directories and read-only SQLite metadata are
read; archive Git and source files are untouched. An unreadable ledger or a bounded
read timeout displays incomplete totals instead of a misleading zero.

The captured-page browser and complete document reader are separate mobile screens
with normal vertical scrolling. The searchable page list preserves its position
when returning from a document. The reader has dated Wayback links, capture-version
selection and thumb-reachable Previous/Pages/Next controls. On desktop, labeled
page/document panels appear side by side with independent scroll controls. Whole-site
approval appears on the site's decision screen, outside the reader. URL navigation
retains the stage, site, selected page and version across reloads; browser Back,
status polling and manual refresh preserve page filters, scope drafts and reading
positions. Sources are plain extracted text; archived HTML/scripts/images never run.

Capture cards and workspaces refresh every five seconds with remaining captures,
processed/known totals, a percentage bar and a running ETA, alongside actual file,
byte and URL counts. Totals can grow as catalogs and supporting files are discovered;
unknown totals stay indeterminate. ETA uses up to 30 recent progress samples from
the current attempt, excluding reused sources and paused time. A slow replay cannot
make the countdown claim completion. Counts persist across reloads and pauses;
resume recalculates the estimate. Multi-site operations are labeled as batch
progress with the current site named. Capture errors stay in Capturing
with an explicit Resume; queued sites explain why they wait. Indexing separates
publication, waiting for existing Jobs, and AI enrichment/import, with no invented
percentage. Publication and indexing failures remain in Indexing with a contextual
Retry action. Technical Job details expand separately and remain open across polls.
Discover & grade is a primary action at the top of Candidates, with the explicit
50-site target, one-hour deadline and $2 total run limit beside it. A visible
explanation names any capture, discovery or publication occupying the shared worker; polling makes the button available
when that work finishes, without starting a run. Paused discovery can be resumed
there within its original limits, deadline and budget. Candidates polls every five
seconds while visible: completed site checks appear immediately, with a progress
bar, qualified/checked counts, current phase, remaining time, estimated usage and
reserved spending. Changing the grade slider filters the view; it does not change
the saved grade target of an active run. More contains recent operations,
capture limits and secondary history views. There are no scheduled paid runs.

Add a site, also in Candidates, accepts an original HTTP(S) URL or a Wayback
replay/calendar link. Bare addresses use HTTP; original protocol, path case,
escaping and query order are preserved. Wayback links identify the original page,
with the submitted link retained as provenance; sampling still prefers 1999–2001,
then 2002–2006. Submitting explicitly permits checking **one site, up to $2**.
The single worker checks archive/account coverage, takes the usual bounded samples
and runs the normal Luna grader. It does not spider out to other sites. Results
join Candidates with the ordinary evidence, sources, grade and capture-scope
review. Missing sources or invalid grades remain unapproved.

An existing ungraded candidate has **Find samples & grade** or **Grade source
evidence** on its card and workspace. This explicitly checks one site with a
maximum $2 Luna budget, rechecks coverage before spending, and uses existing
hash-verified source files when present. Otherwise it requests exact Wayback
samples through the same serial, throttled downloader and date tiers. Sampling
failures and unresolved URL identity are shown directly, instead of the generic
"graded source evidence required" message. The card shows queued/coverage/sampling/
grading progress. **Retry evidence & grading** resumes that operation's original
Luna and Wayback budgets and reuses a saved valid grade. It never auto-approves
capture. Concurrent human decisions or changed source manifests prevent a stale
result from replacing the current candidate.

`POST /api/check-candidate` requires `id`, `manifest_sha256` and an explicit
`max_usd` up to 2. It returns the new or reused operation ID and original cap.
Private run ledgers retain evidence and spending across interruption; saved sources
are hardlinked rather than copied. Completed grades appear in Graded sites when
they meet the selected minimum. No paid check starts from a filter change or poll.

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

Newly claimed captures use [complete-files-v1](full_capture.py). Ordinary site,
account and directory scopes use 200-row, uncollapsed CDX **prefix** inventories;
exact-page scopes use exact listings. Every successful (HTTP 200) file type is
eligible, including files with no surviving HTML link. HTTP/HTTPS and www/bare
aliases retain their exact source identities while sharing the approved path or
account boundary. External supporting files require verified HTML/CSS references;
their exact URLs are inventoried without crawling the surrounding external site.
The custom board engines verify discussion ownership before collecting linked
supporting files. All available dated versions in inclusive UTC **1999-01-01
through 2006-12-31** are requested, including identical-content versions and
previously sampled URLs. Actual replays must match the requested URL and date;
a nearest-date replacement is reported as a coverage gap, never relabelled.

The new ordinary capture path has no page/file-count cutoff. SQLite checkpoints
each catalog page and file, and binary downloads and publication hashes stream
from disk. Its cumulative transport allowance is 100,000 requests, 50 GiB and
seven days of active acquisition, with three-second request spacing, 128 KiB/s
throttling and bounded 422/429/server backoff. A 256 MiB free-space reserve and
per-request disk-space checks protect staging. Limits or transport failures keep
the site paused in **Capturing**, never move a truncated subset into Review.
Explicit **Resume** extends an exhausted transport allowance while retaining
all consumed requests/bytes/time and completed checkpoints. It cannot renew a
Luna/discovery budget. Board catalog/file safety ceilings are 10 million/1 million
records for new portal work and can extend on explicit resume; board source
ownership parsing is bounded to 32 MiB per HTML page.

Review appears only after every catalog and pending version has been checked.
Missing or substituted replays and supporting URLs with no captures remain
visible as dated coverage gaps. This describes what Wayback makes available,
not proof that it archived every original file. Images, CSS, scripts, archives
and other assets have a separate **Supporting files** browser and an original
file download; nothing from the archived site executes in the portal. File lists
paginate at 100 URLs. Polling uses compact batch metadata and reuses the selected
manifest by hash. Publication preserves the complete file set in one site commit;
HTML/plain-text pages (up to 32 MiB with nonempty text) receive default AI enrichment
and indexing, while supporting/binary files remain preserved without Luna calls.

Existing claimed captures and publication/indexing manifests retain their saved
policy. Unapproved ordinary-site legacy reviews offer **Regenerate full capture**:
they return to the unlimited capture queue with the same approved scope and a
60-second grace period. **Undo regeneration** returns the original review until
the worker actually claims it. Each site gets an independent `complete-files-v1`
acquisition, reusing source-verified files and carrying the original cumulative
transport usage. The original manifest remains immutable; no sources are copied,
and a fresh full-scope CDX inventory is required even when the legacy traversal
claimed completion. This also repairs two-sample sites from older mixed batches,
whose earlier sites could consume the shared 100-file allowance. Individual,
bulk and legacy-API indexing approvals reject these incomplete legacy captures.
Publication/indexing approval is required after full acquisition completes.
The source/hash-bound `POST /api/continue-capture` takes the review `id` and
`manifest_sha256`; polling and deployment never queue regeneration automatically.

Already claimed legacy HTML traversal keeps its old 20-URL,
100-file, 1-MiB response, 64-MiB/500-request/30-minute bounds and explicit subset
notes. Discovery sampling is still small and prefers 1999–2001 evidence; this
preference never truncates a newly approved site's date range.

If an entry URL has no successful HTML records, sampling inspects a bounded
redirect/error catalog. An archived redirect can provide evidence only after
its same-scope/account destination and redirect chain are verified. The destination
is independently listed and sampled, retaining its actual URL/date and the
entry-point provenance. For example, `www.solusekro.com/` redirects to `/eq/`;
its 2000/2001 destination captures are valid evidence. A Wayback calendar containing
redirects or HTTP errors is no longer described as having no captures. A redirect
outside the saved scope has a visible scope-edit/retry action. Retry uses the
current scope with the original operation, source caches and budgets.

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
approved. **Discover & grade** explicitly starts another run, targeting 50 **new
sites at or above the selected minimum grade** (default 2). Low grades, unavailable
samples and duplicates do not consume target slots. The worker replenishes its
shortlist from cached links and newly graded sources until it reaches the target,
one hour from its first claim, or the **$2 total run cap**. Reservations can stop
the next request before actual estimated charges reach $2. Exhausted links are a
visible completion reason; transport/authentication failures pause explicitly.
Original deadlines survive restarts and resume, including downtime. Sites already
found count toward the target even if reviewed while discovery continues. Old
operations without the fill policy keep their fixed original shortlist.
Wayback keeps one serial persistent downloader, a three-second delay, 128 KiB/s,
1 MiB responses, and cumulative ceilings of 1,200 requests/128 MiB within the
remaining hour. Seed/staging source reads retain their existing 66-read/16 MiB
limits, with cached local inventories and links reused across runs.
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
protocol and default-port variants as site aliases, including archive folders
ending in `:80`, `:443`, `_80` or `_443`. Nondefault ports remain distinct. Source
URLs, capture identity and published paths remain exact. No page blobs or checkout walks are needed.

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

## Candidate failures and exclusions

Candidate failures remain visible on both the list card and a highlighted panel
above recovery controls in the site workspace. Missing captures, mismatched
original URLs, oversized grading sources, request throttling and saved-budget
limits explain what happened and what action can help. Missing-source panels show
the exact original URL (including its query), link to Wayback's capture history,
and distinguish retrying that URL from submitting an actual EQ site. Safe original
diagnostics remain available under **Technical detail**. Polling updates failures
even if the candidate hash is unchanged, clears old warnings during a new check,
and never starts work automatically.

The FreeServers `cgi-bin/redirect?id=ezboard-r1` link found in Ezboard footers is a
hosting signup advertisement. Discovery and manual/evidence checks exclude this
specific campaign; genuine hosted accounts and other redirect targets remain
eligible. Unapproved legacy suggestions move to History with the reason and keep
their evidence. Active checks and approved records are preserved. No human
dismissal is fabricated and no Luna request is made to classify this known advert.

## Ezboard capture

Ezboard candidates represent a complete board across historical servers.
Forum/message submissions resolve their parent before sampling/grading creates
a candidate; existing boards are reused. New board candidates default to
**Whole Ezboard**, which uses paginated board/forum capture catalogs and the
serial downloader. Newly claimed work uses the complete-file policy above,
including source-linked images, stylesheets and downloads. Legacy claimed work
keeps its 2,000-capture/256-MiB/one-hour budget. Existing approved scopes
and indexing Jobs are preserved. See the [Ezboard guide](../../scripts/archive-crawler/EZBOARD.md)
for the verified URL forms, operator commands, limits, migration and source
validation.

## SitePowerUp capture

SitePowerUp candidates represent one numeric `BoardID`, including links to
individual `Action=Reply` messages. Discovery and manual submissions reuse that
board identity across `www`/bare hosts and parameter order/case, while preserving
the exact source URL and submitted provenance. Coverage recognizes both current
query filenames and the archive's older `_and_` filenames using bounded, shared
Git metadata. Unapproved legacy message suggestions consolidate without copying
sources; indexed/approved boards and active checks retain their records.

The **Whole SitePowerUp board** scope captures available dated indexes, messages
and pagination from 1999–2006 through serial, paginated CDX requests. It retains
query strings in `websites/<host>/<timestamp>/<decoded path>` destinations and
verifies the BoardID against each discussion source. Other boards and posting/admin
actions are excluded. Newly claimed work adds verified supporting-file references
and uses the complete-file policy above; existing claimed work retains its
2,000-capture/256-MiB/one-hour policy. Each board has its own review. Existing exact-page
approvals do not widen. Publication and default AI-enriched indexing still require
the usual whole-site review approval. See the
[SitePowerUp guide](../../scripts/archive-crawler/SITEPOWERUP.md) for format
evidence, operator commands, supported views and limitations.
