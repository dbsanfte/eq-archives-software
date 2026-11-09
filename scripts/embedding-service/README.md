# Standalone CUDA embeddings for indexing

The Ubuntu GPU host runs the **same EmbeddingGemma 2 Q8_0 weights,
768-dimensional normalized vectors, document/query prefixes and model alias** as
the Vulkan service. All indexers use CUDA; live search and MCP use Vulkan.
The separately managed CUDA server is reachable on the wired LAN only, at
`192.168.68.108:8081`, with the existing Nomic API key. It is not a paid API.
The root Kubernetes Kustomization manages a selectorless
`nomic-embeddings-rtx3080` Service/Endpoints pointing to this host, without changing
the Vulkan Deployment. The JSON patch removes the Service selector added by
Kustomize's shared labels; the rendered-route CI check verifies that routing stays
on the external address and does not select local cluster pods. Reserve the laptop's wired DHCP address in the router;
update both `cuda.env` and the endpoint manifest if its address changes.

The current eqvm LAN address is `192.168.68.53`; CUDA is at `192.168.68.108`.
For a LAN renumbering, preserve active indexing Jobs and their sources. Update the
GPU host's installed `cuda.env`, restart only its embedding service, and update the
selectorless Endpoints address. If k3s still reports the old node address, refresh
the k3s server process: its normal service restart preserves running containers
(`KillMode=process`). Never use `k3s-killall.sh`, drain the node, or recreate the
indexer to change these addresses. Verify the same Job/pod UID and restart count,
updated node/Flannel addresses, authenticated inference and resumed checkpoints.
Curation derives its bind address from `status.hostIP`; the deployment readiness
check discovers the ready pod's host address instead of a fixed IP. Commit the
new CUDA binding and Endpoints together so future deployments retain the route.

## Host prerequisites and installation

Use Ubuntu's signed NVIDIA server driver compatible with the host kernel and
Secure Boot, Docker, and NVIDIA Container Toolkit with **native CDI** support.
This installation uses driver 580.178.04, toolkit 1.20.1 and Docker 29.1.3.
Enable `nvidia-cdi-refresh.path`; `docker run --device nvidia.com/gpu=all` must list
the CUDA GPU. Keep Secure Boot enabled. Do not raise the laptop's 80 W power limit.
The CUDA image in `cuda.env` is pinned to the same llama.cpp 1052f7e1d626 revision as
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
  --url http://192.168.68.108:8081/v1/embeddings \
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
before a document can be saved. Text passages retain a 480-token total request
budget, including the real title, section context and special tokens.

`REINDEX_WORKERS=4` starts isolated handler/embedding clients for the primary CUDA
endpoint. The production renderer removes all inherited `REINDEX_SECONDARY_*`
settings and sets `OPENAI_ENDPOINT_EMBEDDINGS` explicitly to CUDA, so a previous
embedding override cannot send background indexing to Vulkan. The four workers
share one bounded ES scan and read-only source tree. Curation imports and saved
indexing Job templates also use the CUDA endpoint. Public search and MCP retain
their Vulkan endpoint independently of laptop availability.
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
  --name reindex-cuda-<date> \
  --image eqarchives-indexer:rtx3080-<merged-40-character-sha> \
  < /private/running-job.json > /private/new-job.json
```

Import the image into k3s, server-side dry-run, probe exact remaining IDs and verify
metadata/vectors. Create the replacement at parallelism zero before suspending the
old Job. Wait for the old pod to stop, then raise replacement parallelism to one.
Keep the original recovery runner, immutable ConfigMap, credentials, previous Jobs,
logs and prepared source checkout; never repeat source preparation. Curation's
broad-writer exclusion for background maintenance remains in effect throughout. Watch both backend counters,
GPU utilization/power and ES resource usage. No LLM enrichment or API spend is added.

## EmbeddingGemma migration

The production text embedding model is EmbeddingGemma 2 Q8_0, 768 dimensions,
canonical alias `embeddinggemma-2-768-v1`. CUDA and Vulkan image pins use the same
llama.cpp revision (1052f7e1d626); public model/runtime hashes live in `runtime.env`
and `cuda.env`. Both GPUs fit the same 310 MB text model. Image inference uses a
separate CUDA companion with the pinned projector in `vision.env`. Never mix
Gemma and Nomic vectors in the same semantic query.

1. Validate both pinned runtimes privately, including finite normalized vectors,
   cross-host cosine, near-480-token batch/serial identity, oversize rejection and
   recovery. Preserve the prior systemd runtime/configuration and model files.
2. Test `indexer/test` on Python 3.13 with **both** pinned tokenizers installed.
   Build the operator image using `Dockerfile.reindex` and the exact previously
   verified image as `BASE_IMAGE`. Tag it `eqarchives-indexer:rtx3080-<merged-sha>`;
   record its immutable local image ID, and import it to k3s containerd.
3. Save the old Job JSON and logs privately. Render a new Job with
   `render-reindex.py --gemma --name reindex-gemma-<date> --image <tested-image>`.
   It starts with `parallelism=0`, retaining the source checkout, credentials,
   recovery wrapper and no-paid-enrichment policy. Create it **before** suspending
   the old Job, so the curation background-maintenance exclusion remains effective.
4. After normal CI/deployment completes, suspend the old broad Job as an explicit
   operator handoff. Wait for its pod to stop; do not delete the Job, repeat source
   preparation or allow Nomic/Gemma writers to overlap. Install the new CUDA
   `runtime.env`, `cuda.env` and `service.py` and restart the embedding service.
   Verify authenticated inference and the systemd health check before proceeding.
5. Run exact-ID migration probes with the tested image against CUDA, including a
   long text, existing summary/OCR and an old extraction version. Inspect complete
   vectors and the optimistic update, and verify keyword plus model-filtered
   semantic searches. Probes must not regenerate Luna enrichment.
6. Set the prepared new Job's `parallelism=1`. Use four isolated network workers,
   two CPU preparation processes and `GEMMA_EMBEDDING_BATCH_SIZE=8` with profile
   `cuda`, initially retaining the benchmarked 64-slot/512-token server profile.
   There is no intentional request sleep. Read-only sources remain at
   `/mnt/samsung/eq-archives-html-reindex`; current extraction versions reuse stored
   full text to avoid source I/O. Curation also routes exclusively to this CUDA
   endpoint, using Gemma's prefix/tokenizer and marker on newly imported documents.

`reindex_embeddings.py` scans stable IDs without a long-lived PIT. It preserves
source text, existing enrichment/OCR, dates and provenance; summaries/OCR are
chunked and normalized when aggregated. One optimistic partial update replaces
**every searched vector** and then marks the model version. Invalid/incomplete
vectors cannot mark readiness; conflicts and document errors are counted and do
not hold other workers. Missing sources are counted, retain original text/chunks,
and never claim repaired extraction. Unusable old vectors with no corresponding
text are cleared during replacement. Empty sources are not invented.

`embedding_model` and `text_chunking_version=eq-passages-480-v12` together are the
text resume checkpoint. Older Gemma records remain eligible for the passage
upgrade; `REINDEX_PRIORITIZE_UNCONVERTED=true` finishes the old model frontier
first, then upgrades previously converted Gemma records. Count model conversion
and passage-policy completion separately. The recovery wrapper waits for infrastructure and resumes
completed versions after temporary outages. A final nonzero failed count requires
inspection and another checkpointed pass; never report it as complete. To check
progress, count remaining documents using `remaining_query()` and count the exact
`embedding_model` marker; read rate/counters from the Job's bounded progress logs.
Do not equate a Job exit or a process-local processed count with total completion.

Website source repair also requires extraction checkpoint
`html-2026-11-05-regional-decoding`. The shared offline decoder handles CP932 Windows
extensions, Shift-JIS, EUC-JP and ISO-2022-JP using actual byte/script evidence.
Misdeclared UTF-8 archive wrappers do not override validated bytes. Bounded damaged
sequences remain visible as U+FFFD and have counted source-hash-bound provenance;
excessive damage fails visibly. Original capture bytes are unchanged. The migration
retains completed passage checkpoints as history and reuses vectors when reader text and the complete
search plan are unchanged. Corrupted reader text and searched vectors are replaced
atomically, preserving enrichment/dates and correcting only source-proven garbled
titles. Mail/newsgroup and binary-document extraction checkpoints remain intact.
Use the normal prepared-Job handoff; never repeat source preparation or call Luna.

The frontend and MCP use Gemma query prefixes and restrict all semantic branches
to the marker. Keyword search still covers the entire index. Deployment validates
that a zero-ready initial corpus really has zero model checkpoints; once ready
records exist, the vector-only smoke check must return a match. Test search
relevance on the completed corpus before claiming an improvement. Preserve old
models/Jobs for diagnosis; reverting runtime alone after partial migration is
unsafe because the embedding spaces differ even though both have 768 dimensions.

## Content-aware passages and images

`indexer.passages` builds a separate, hashed search view and retains full reader
text and archive sources. Generic HTML uses explicit content landmarks, headings,
paragraphs and data tables; uncertain layouts keep their content. Allakhazam,
Everlore and CastersRealm remove identifiable layout navigation. Ezboard and
supported forum post containers retain author/date context; newsgroups and
mailing lists retain quotation boundaries and source attribution. RSS entries
have separate boundaries. Table chunks retain complete rows where possible and
repeat column context. Oversized rows/prose split with overlap. Every complete
request is checked with the pinned Gemma tokenizer against 480 tokens.

Policy `eq-passages-480-v8` recognizes phpBB, WWWThreads, Snitz and vBulletin by cooperating
HTML features across domains, along with Ezboard's Author/Comment rows. Each post
keeps its own speaker/date, quotes and authored data tables. Signature separators,
profiles and thread controls stay outside those message bodies. Partial/damaged
recognition keeps unmatched bodies or falls back to the complete generic view;
unregistered authors are retained. Posting/login forms and recognized error or
disabled-site pages receive no semantic vectors, including summary/OCR vectors.
vBulletin uses complete message-comment regions or per-post IDs in full threads,
and matched username/posttext pairs in stripped archive pages. It retains attached
file labels and authored tables/quotes, excludes profile/signature controls and
falls back on damaged boundaries. Earlier v4 checkpoints remain eligible for this
upgrade; source preparation, dates, readers and enrichment remain intact.
Full reader text and existing enrichment remain intact.

Separate data tables get separate column contexts from meaningful rows rather
than empty Markdown framing headers. Short prose sections pack together within
the request budget; post, feed and quote boundaries remain independent. Fenced
code is not interpreted as table/quote markup. A numbered author reply following
wrapped quoted text starts a new attribution scope. These heuristics are tested
against structural reproductions of live captures and sampled unrelated layouts;
they are not a claim that every historical forum theme is recognized.

The reindex progress log includes passage, post/table/quote, short-passage and
semantically excluded-document counters for acknowledged writes. Review these
alongside bounded exact-record samples; small chunks can be legitimate short
replies. Keep private source snapshots/audit outputs outside both repositories.
Older checkpoints remain valid history and are checked against the current passage policy.
Use the normal pending-Job handoff, retaining source preparation and all previous
Jobs; the independent image backfill and embedding services need no restart.

Passages store offsets into the selected search view, title, context and kind;
`text_passage_provenance` records source/view hashes and exclusions. These offsets
are not offsets into `text_full`. JavaScript/CSS and recognized archive access
errors retain their archival reader but receive no semantic passages. Placeholder
summaries never receive vectors. Existing meaningful summaries/OCR are reembedded
atomically without regenerating enrichment. Fresh imports use the same policy.

`install.sh` also enables `eqarchives-vision-embeddings.service` and its independent
one-minute real-inference health timer. The companion binds only the CUDA host's
LAN address on **8082**, uses the same text weights, alias and private Nomic key,
one 2,048-token slot and the checksum-pinned Q8 projector. It does not restart or
replace the 64-slot text service. Kubernetes routes it through selectorless
`gemma-image-embeddings`; `GEMMA_IMAGE_ENDPOINT` can override this private route.
Three failed health checks restart only the image service.

Visual embeddings use Gemma's structured image input, without a text prefix, in
the same 768-dimensional space as text queries. Supporting raster files in new
approved captures are create-only indexed without Luna calls. Readers and assets
retain their source identity. Inputs are bounded to 20 MiB/40 million pixels,
EXIF-oriented, fitted within 1,600 pixels and alpha-composited with contrast.
The first visible frame among eight GIF frames is used. Tiny, transparent,
corrupt, unsupported or oversized files retain typed per-file diagnostics;
audio, video and SVG are not directly embedded. Search and MCP include the
model-filtered `image_vector` branch. Deployment installs its additive mapping
before releasing a frontend that queries that field.

For the initial raster backfill, reuse the existing prepared source checkout;
do not clone, fetch or walk it. Prepare `/mnt/samsung/eqarchives-images` with mode
0700 and owner matching the indexer, then render a separate pending Job:

```bash
python3 scripts/embedding-service/render-reindex.py --images \
  --name reindex-images-<date> --image eqarchives-indexer:rtx3080-<merged-sha> \
  < /private/running-job.json > /private/image-job.json
```

The image worker streams local Git trees through a remote-free reader, checks
source bytes against pinned blob identities and never overwrites an existing
document. Its SQLite per-host cursors, missing-source diagnostics and bounded
blob-vector cache persist under the image-state volume. One serial image worker
can run alongside text indexing on CUDA; it performs no paid enrichment. Use
`index_images.py --host <host> --limit <count>` for bounded probes. Preserve its
state and exact source commit across retries; a changed revision is rejected.


### Forum template and missing-text recovery

Policy `eq-passages-480-v6` extends shared vBulletin recognition to guest menus,
word-form dates, numbered post boundaries in malformed themed wrappers, and paired
vB 2 collapsed/expanded views. UBB.classic uses cooperating Author/Topic columns,
posted timestamps and body separators; missing cell closures are recovered only
within their original row. Lithium uses its message/user/date cells independently
of hostname. Incomplete recognition keeps all messages via conservative fallback.
Allakhazam chooses actual entity headings instead of sidebar labels and supports
both stacked and side-by-side comment bodies. Encoded external ad/noscript
fragments and verified empty posting forms are excluded only from the search view;
quoted code, tables, attachment labels, biographies and surviving quotations remain.
The reader converter flattens excessive formatting/container nesting while keeping
visible text, data cells, code and image links, under extraction checkpoint
`html-2026-11-05-regional-decoding`.

The upgrade checks exact saved text/title/context/kind/offset tuples as well as
source/view provenance before reusing v4/v5 vectors. Matching records get the new
checkpoint without inference; changed records replace all searched vectors in one
optimistic update. Existing enrichment and capture/index dates remain intact.

`index_websites.py` recovers missing HTML/plain-text/XML/JSON website documents
from the already prepared source checkout. It streams local Git trees, binds raw
bytes to the committed blob, checks existing IDs in batches, and uses create-only
writes. It never calls paid enrichment, overwrites existing records, walks the
checkout, changes archive Git, or repeats source preparation. Its private SQLite
state retains the selected hosts/revision, per-host metadata cursors and per-file
diagnostics. A failed extraction or oversized write does not stop later files;
authentication, transport and invalid embedding responses remain visible. The
recovery wrapper retries temporary dependency outages with checkpoints intact.
Opaque routes such as Ezboard `.showMessage` are candidates; extensions never
establish HTML identity. MIME-confirmed non-text responses (including dynamic
avatars) are counted as exclusions, with private diagnostic reasons. Files above
the explicit 8 MiB bound and invalid captures remain unresolved source gaps; no
truncated content is marked complete. `--retry-failed`
explicitly revisits saved gaps after correction. A nonzero final gap count is an
incomplete import, never a successful full repair.

For an authorized run, prepare `/mnt/samsung/eqarchives-text-import` once as UID
1000, then render a separate pending Job using the **original text reindex Job**:

```bash
python3 scripts/embedding-service/render-reindex.py --name reindex-missing-text-<date> \
  --image eqarchives-indexer:rtx3080-<merged-full-sha> \
  --text-host necro.eqclasses.com --text-host www.thesteelwarrior.org \
  --text-host www.eqatlas.com --text-host eqcleric.gameglow.com \
  < previous-text-job.json > missing-text-job.json
```

Verify exact source-backed records in a disposable index with the compiled image
first. Follow protected CI/deployment, then create the rendered Job at parallelism
zero and activate it only after verification. It has one CUDA import worker and
its own persistent state; the full reindex keeps its independent checkpoints and
the image Job stays untouched. Add each approved archive host explicitly; changing
a saved host scope or source revision requires a separate state directory. Monitor
created/existing/unsupported/unavailable/failed counters and the private gap ledger.
New records carry archive/source hashes, exact source identity, capture date,
complete reader, current Gemma/passages/extraction checkpoints and local vectors.
This is source recovery, so normal curation approval and daily Luna policies are
unchanged. Existing-document reindexing alone cannot repair absent records.

### Mixed legacy punctuation and large vector writes

The shared offline decoder uses local evidence and inherited text-node context,
with declarations/detection as hints. It can mix UTF-8, Shift-JIS/CP932, EUC-JP,
ISO-2022-JP and a declared legacy script within a file, including whitespace-bound
runs in the same text node and plain-text lines. Strict codec success alone does
not establish the correct interpretation. Independent local evidence is required
before switching encodings; ambiguous short runs inherit context, and unknown
bytes retain visible replacement markers within the existing damage bounds.
ISO-2022 escape runs retain their state within each text node. HTML-escaped
ASCII bytes are restored only inside shifted JIS runs before decoding; ordinary
HTML entities remain untouched. Repairs and genuine truncated sequences have
separate counters, isolated across concurrent workers. Provenance records the
exact source hash, byte ranges, encoding decisions and replacement counts. Compatible adjacent ranges are coalesced to bound metadata.

`source-decoding-v2` and `html-2026-11-05-regional-decoding` let the text Job revisit
older website extraction, including mixed pages that previously passed a single
codec. The model and passage policy stay unchanged. Identical reader/passage
requests reuse existing vectors and update only the extraction/provenance
checkpoint; changed readers/views replace vectors atomically. Existing
mail/newsgroup checkpoints, enrichment, source identity and dates remain intact.

Indexer and curation writes use `indexer.vector_json` to encode dense vectors with
nine significant digits after a float32 conversion. Every finite float32,
including signed zero, round-trips exactly; metadata keeps its ordinary precision.
This avoids expanding each float32 into a long Python double spelling and keeps
large complete documents within the existing Elasticsearch HTTP bound. JSON,
bulk NDJSON and create-only capture imports share the encoding. Nonfinite or
unrepresentable vectors fail before transport. No passages are dropped and no
model, passage-policy or extraction checkpoints change for this transport repair.

The operator Dockerfile coalesces source updates into two additional layers so
an exact-predecessor update can be built without a layer for each copied module.
Use the normal pending-Job handoff and retain the previous image and Jobs.

### Localized forum and advertising projection repairs

Policy `eq-passages-480-v6` recognizes Ezboard column skins from profile/reply
links, dates and header structure instead of translated label lists. Threaded
single-message views keep their own author/date/body, and separate reply indexes
remain in the archival reader. Guests, damaged headers, attachments and real
nested tables are retained; ambiguous layouts fall back to complete content.
Deleted-post notices receive no semantic vectors.

Allakhazam's side-by-side comments keep an empty body separate from profile and
rating panels. Surviving authored subjects remain searchable; anonymous authors
do not inherit moderation scores. Real body tables, quotations and attachments
remain intact.

A bounded, non-executing script scanner repairs prematurely closed inline scripts
across all sites. Only a quoted closing tag followed by a proven outer boundary
is isolated; uncertain or truncated markup stays unchanged. Source readers and
bytes are untouched, and authored escaped code/quotations remain searchable.

Use the normal protected deployment and pending-Job handoff, preserving sources,
import cursors and prior Jobs. Exact unchanged v4/v5 request metadata and source/view
provenance permit checkpoint-only updates without reembedding. A matching original
byte hash also permits equivalent CRLF/LF HTML representations when the full
reader, selected view and every request field are identical; changed passages
replace all searched vectors atomically. No paid enrichment runs in this repair.

### Corpus forum and database format repairs

Policy `eq-passages-480-v7` extends the shared format adapters with complete
Snitz topic-review/composer rows and older normal topics. A captured quote is
preserved alongside reviewed messages; damaged or mixed layouts cannot select
only the textarea. Original source positions bound text outside broken table
rows. Legacy vBulletin comment regions use their header/profile/body pairs and
source token boundaries; archive author/date/body pairs need no numeric IDs.
ASP.NET Forums PostList roles, UltraBoard message blocks and phpBB/Lithium
printer layouts receive separate attributed posts across hosts. Everlore's
submission/date/body rows retain their surrounding entity data while isolating
rating widgets and navigation. Unknown content keeps the conservative fallback.

Template cells do not become invented data grids, and literal fence-like forum
signatures cannot absorb later authors. Genuine tables, quotations, attachments
and code remain. Generated deleted/restricted/missing-thread/PHP failure layouts
produce no semantic vectors, regardless of their URL action. Platform/body
evidence protects real discussions of those errors; reader text stays intact.

Both capture imports and legacy workers deduplicate exact embedding requests
within each passage plan. The complete document prefix/title, author/context
and body are part of the identity; reuse never crosses model calls. Every source
occurrence still receives a separate offset-bound result and vector copy.
Incomplete or nonfinite unique batches fail before a document can be saved.
This reduces inference for captured repetition without truncating content.

Use the protected deployment and pending-Job handoff above; retain prepared
sources, import cursors, all previous Jobs and the independent image backfill.
Both the model and v7 passage checkpoint are required for completion. Exact
unchanged v4/v5/v6 requests can upgrade their checkpoint after the existing full
reader/source/view checks; changed plans replace all searched vectors atomically.
The unfinished model frontier retains priority. No paid enrichment runs during
this repair, and existing corrupt/missing-source diagnostics remain visible.

### Bounded shared text-embedding cache and rejection ledger

The v8 passage repair recognizes complete phpBB database-error layouts before
applying the small-error-page limit, retaining readers and genuine SQL articles,
quotes and forum posts. Explicit bodies nested inside malformed heads are
recovered before metadata removal. A separate generated diagnostic can be removed
without erasing real appended replies or quoted troubleshooting. `source-decoding-v3` requires varied Japanese script
before trusting an undeclared detector hint; repeated Western symbols cannot
establish Japanese or a second regional codec. Explicit Japanese declarations,
local mixed-encoding recovery and bounded damage checks remain. The website
reader checkpoint is `html-2026-11-05-script-evidence`. Verified identical raw
bytes and decoding provenance can advance the previous regional-reader checkpoint
without extracting the reader again; exact unchanged v4/v5/v6/v7 passage requests
can reuse their vectors. Changed readers/views replace searched fields atomically.

Text workers and newly submitted curation imports share private SQLite state at
`/mnt/samsung/eqarchives-curation/indexing-state`, mounted as `/indexing-state`.
It uses the existing Samsung-backed curation PVC; continuations mount the same
host directory without changing their source checkout or import cursor. The seed
init step creates only this small directory, including on previously seeded
volumes. It is owned by 10001:10001, mode 2770; workers retain their existing UID
and gain supplemental group 10001. Database files are mode 0660. No archive-tree
ownership walk, source preparation or copy is required.

`EMBEDDING_CACHE_PATH=/indexing-state/embeddings.sqlite` enables the optional
cache. It stores hashes and checksummed 768-dimensional float32 vectors, not
source text or credentials. Keys bind the entire prefixed title/context/text
request to the model alias, immutable weight/runtime pins and tokenizer checksum.
Both image builds copy the authoritative model `runtime.env` and indexing
`cuda.env` for that identity; a CUDA-runtime upgrade invalidates reuse independently
of the live Vulkan runtime. Every
source occurrence retains its own offsets and vector; a hit never bypasses source
validation, complete-response checks or the document's atomic model checkpoint.

Defaults and environment overrides:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `EMBEDDING_CACHE_MAX_BYTES` | 536870912 | 512 MiB SQLite database page cap |
| `EMBEDDING_CACHE_MAX_ENTRIES` | 100000 | Maximum live vectors, further bounded by the byte cap |
| `EMBEDDING_CACHE_HALF_LIFE_SECONDS` | 86400 | One-day half-life for accumulated heat |
| `EMBEDDING_CACHE_MAX_IDLE_SECONDS` | 604800 | Expire an entry seven days after its last hit |

On a hit, heat decays exponentially over the elapsed idle time, gains one hit,
and refreshes last-use time. Capacity eviction uses an indexed equivalent of the
current decayed heat, retaining frequently used inputs over colder ones without
scanning all cached vectors. Old heat fades; hard idle expiry applies even to a
formerly hot entry. Expired entries are unusable immediately and reclaimed in
bounded batches during cache activity. SQLite reuses freed pages instead of
vacuuming the entire file. An 8 MiB WAL high-water mark gates further writes and
attempts a bounded checkpoint; journal growth is limited to that threshold plus
one bounded transaction. A long-lived reader cannot grow the journal indefinitely.

Use consistent bounds across clients. A changed heat half-life needs a fresh
cache path for all clients; incompatible policy is reported and bypassed.
Contention, corrupt entries or unavailable optional cache storage fall back to
inference, with counters/warnings. Invalid or incomplete inference responses fail
before entering the cache. No SQLite transaction holds an inference/ES call.
Cache hits/misses/evictions appear in acknowledged reindex progress; imports print
their cache counters. This adds no paid enrichment.

`REINDEX_DIAGNOSTICS_PATH=/indexing-state/reindex-failures.sqlite` is independent
of the disposable cache. It persists rejected IDs, actual source hashes when
available, model/policy, safe reason categories, exception type, first/last times,
attempts and originating Job. It never stores exception payloads or source text.
Successful acknowledged repairs retain the rejection history and record the
resolution policy/hash/Job. Missing sources and failed optimistic updates remain
pending. Metadata contention retries only the ledger transaction after external
work; other ledger failures stay visible. A small pending-ID index avoids hashing
or querying SQLite for every successful record. Existing import gaps retain their
own persistent inventory.

Inspect an existing ledger read-only, without running inference or changing archive
documents. Missing/unreadable storage is an error; inspection and retry commands
never create an empty replacement ledger:

```bash
python -m indexer.reindex_diagnostics --status pending --limit 100
python -m indexer.reindex_diagnostics --status all --limit 100
```

During an explicit operator retry window, the same pinned worker can run
`python reindex_embeddings.pyc --retry-failed --limit 1000`. This selects at most
1,000 distinct pending IDs and never turns an empty exception list into a broad
scan. Already-complete IDs can resolve their diagnostics without inference.
Content/integrity failures retain their failure status and never hold later
records; the ledger does not pretend they completed. Continue using the prepared,
pending-Job handoff, preserve all predecessors/cursors and leave the independent
image Job unchanged.

## Verified discussion metadata

`index_discussions.py` adds only `discussion` metadata to already indexed forum
captures. It uses the existing prepared archive checkout read-only and a private,
remote-free Git-tree inventory. It never prepares/clones sources, fetches missing
blobs, embeds text, pays for enrichment or restarts an indexing Job. Deploy the
additive [schema](schema.json) through the normal frontend deployment first.

The operator performs two independently checkpointed passes: learn source-proven
Lithium RSS memberships, then annotate recognized forum pages. phpBB, vBulletin,
Snitz and Ezboard require native topic links and structural message evidence.
Conflicting memberships stay ambiguous. Proofs include exact source/evidence IDs
and hashes; titles do not establish identity. Updates use Elasticsearch sequence
numbers and primary terms and touch only `discussion`, preserving dates, reader
text, enrichment, vectors and completion checkpoints during concurrent ingestion.

Run the tested curation image as an operator with private credentials, the source
checkout mounted read-only and a separate private writable state directory. The
CLI requires `--repo`, `--state`, `--catalog` and one or more literal `--host`
values. For example, inside that isolated environment:

```bash
python /app/index_discussions.py --repo /sources \
  --state /work/sony-inventory.sqlite --catalog /work/sony-discussions.sqlite \
  --host eqforums.station.sony.com --es-url http://elasticsearch:9200 \
  --password-file /run/secrets/es_password \
  --max-files 5000 --max-bytes 134217728 --max-seconds 120
```

Retain the exact same state, catalog, source revision and host scope on resume.
Bounds apply per invocation; exit 2 means its bounded slice paused, not failure or
completion. Failed source reads consume file attempts and retain their maximum
byte reservation; `bytes` reports verified reads and `bytes_reserved` also
includes those conservative failure charges. `--learn-only` prepares evidence
without an ES client, credentials or network calls; subsequent normal runs retain
the completed learn pass. Exit 0
means both passes finished with no pending gaps (or learn-only prepared its
catalog). Unindexed documents are skipped and never created. Source/integrity
gaps and concurrent update conflicts are retained privately; `--retry-gaps`
explicitly retries up to 1,000 gaps, and a recovered evidence source resets the
affected annotation cursor. Exit 1 with pending gaps must remain visible. Transport
and authentication failures stop visibly with checkpoints retained. Keep all
catalogs, state, sources and credentials outside both Git repositories.


### Entity, article and utility projection repair

Policy `eq-passages-480-v9` carries source-labelled Everlore database entities
and Allakhazam class subjects into reply requests. Generic link-menu detection
requires several exact control roles and preserves authored descriptions, data
grids and literal examples. Empty vBulletin calendars, structurally proven
pop-up subject directories and frames-only fallback shells have no semantic
passages; an authored event, directory description or noframes body keeps content.
Whole-page utility exclusions clear text, summary and OCR search vectors together
while preserving the reader and enrichment. WordPress skins and semantic articles
with a source title/date/body get separate article contexts and boundaries.
Unknown layouts keep the normal content-preserving fallback.

Normalize CRLF and CR only at the HTML projection boundary, so missing-text
imports, curation imports and reindexing produce identical source-bound views
and requests. Preserve the exact reader/raw source hashes and decoder policy.
Earlier v4–v8 passages can advance without inference only after the existing
reader, source provenance and complete request/offset comparisons succeed.

Use the protected deployment and prepared pending-Job handoff, retaining all
predecessors, prepared sources, rejection/cache ledgers and completed import/image
Jobs. Do not overlap v8 and v9 broad writers: the old policy considers newer
passage checkpoints unfinished. The new run retains unfinished-model priority
and then upgrades older passage policies; it does not call paid enrichment.


### Lithium printer views and source-bound feed entries

Policy `eq-passages-480-v10` extends Lithium's cooperating message/user/date roles
to plain-text and coloured-span printer-view authors. Read only the author line
before its first break; retain guest/damaged messages and complete body content,
quotes, data tables and attachments. Proven empty `notification.body` report forms
are utility pages; saved notes and authored reporting examples remain searchable.
Utility repairs clear all semantic fields atomically, preserving reader/enrichment.

RSS/Atom XML supplies explicit entry boundaries and source subject/author/date
metadata. Render each body separately, retaining unique descriptions/full content,
XHTML structure, quotes, authored code and data tables. Escape literal backtick/
tilde decorations outside actual code before Markdown chunking so a patch-note
separator cannot consume later entries. Keep entry metadata in every chunk's
budgeted context, including sections/tables/quotes inside long posts. Unknown or
malformed XML keeps the conservative reader-preserving fallback. No entity or
remote-content retrieval is allowed. Discussion verification still uses original
raw captures independently of these search projections.

Use the protected deployment and prepared pending-Job handoff. Stop the predecessor
before new-policy writes; preserve all prior Jobs, prepared sources, caches, rejection
ledgers and completed image/import Jobs. The v10 continuation retains unfinished-model
priority, then upgrades older passage checkpoints. Verified identical v9 requests can
reuse vectors under the full existing source/view/request checks. No paid enrichment
or source preparation is part of this update.

When an operator image approaches OverlayFS's layer-depth limit, flatten the exact
verified predecessor filesystem and restore its runtime configuration before
applying `Dockerfile.reindex`. Retain the original image and verify the resulting
compiled worker, pinned tokenizer, complete vectors and unchanged-record reuse
before tagging it with the merged revision. Flattening is local image preparation;
it does not alter running Jobs, archive sources or persistent state.

### EQ Traders, Magelo and generated scaffolding

Policy `eq-passages-480-v11` recognizes EQ Traders article/recipe navigation and
legacy tradeskill/vendor sections, retaining ingredient counts, trivials,
uncertainty, merchant locations and described link directories. Recipe columns
and city/skill headings remain in the budgeted context of continuation chunks.
The site's forums continue through the shared forum handlers.

Recognized Magelo profile layouts bind detached skills/spells/flags table headers
to their captured data. Character identity travels with every chunk; named panels
separate statistics, skills, spells, flags, biography and calculations. Proven
empty panels and UI buttons are excluded; populated biographies, zero/unknown
values, equipment links and unfamiliar layouts remain. No JavaScript is executed
to invent values absent from the capture.

Host-independent rules remove inert calendar/login widgets and proven hosting
advertisement fallbacks, while retaining events and authored text/quotes/code.
Only complete default-server, provider-error and parking templates become status
projections with no semantic fields. Nonempty captured textareas remain quoted
source. Attribution-only post/feed fragments attach to their own next body block,
including large quotations and subheaded articles; separate entries remain
separate. Bold first-row values alone no longer become table-column metadata.
Empty malformed table-row wrappers retain their populated descendant rows;
this restores older EQ Traders clay-conversion tables and unrelated damaged HTML.

### Fifty-site source audit

Policy `eq-passages-480-v12` adds source-evidenced Snowboards/VN, phpBB 1,
old UBB, UBBThreads `font.post` and Magelo forum message handling. Ezboard ad-only
interstitials cannot invalidate a complete thread. A missing vBulletin message
comment may use its own proven post cell, retaining conservative fallback when
the matching header or ownership is uncertain.

Major catalog policies preserve source entity identity, column labels and section
boundaries for EQInterface, EQRankings, MaximumEQ, EQDiva, EQPrices, EQSpoilers,
GuildMagic, Graffe spells and EverQuest Online items. Spells/items remain separate entries;
all raw field values and unknown layouts are retained. Shared calendar, network
navigation and nested advertising-script rules retain authored content.

The LAN curation portal's **More → Chunking audit** dashboard records the fifty
site decisions, bounded source samples and before/after projections. See the
[audit guide](../chunk-audit/README.md) for scope and verification limits. These
are source/structure/token checks, not a retrieval relevance benchmark. Existing
v10/v11 checkpoints remain valid historical progress. New imports use v12;
bounded curation maintenance upgrades old same-model records after existing broad
writers finish naturally. Do not restart the active broad Job for this delivery.
Archive-status projections clear summary/OCR vectors with body vectors atomically,
using the typed projection status rather than an incomplete diagnostic allowlist.

Validate source-bound plans against varied private captures and unrelated layouts.
Keep the full reader, hashes, offsets, enrichment and 480-token request bounds.
An exact v10 source/view/request match may reuse vectors under the existing
validation; changed plans require embedding. Ordinary CI/CD activates v11 for new
imports and leaves the active v10 reindex untouched. The regular curation loop upgrades existing same-model records in bounded slices
after legacy broad writers finish. It yields to foreground imports, preserves all
previous Jobs, sources, cache/rejection ledgers and the independent image workflow,
and makes no paid enrichment calls. Explicit model migrations retain prepared
operator handoffs. See the curation guide for rolling maintenance and diagnostics.
