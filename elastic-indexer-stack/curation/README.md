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
2. Choose scope and **Approve capture**. Deep links default to their containing
   directory and descendants; extensionless paths use that directory itself.
   **Linked page only** and **Whole site / shared account** are alternatives.
   Shared hosts retain their account boundaries. A scope change clears an
   earlier approval and changes its manifest hash.
   Unidentified shared-host accounts remain exact-page scopes, including roots.
3. Select up to five approved sites and capture them together. The worker
   spiders HTML links within the scope, including its entry directory, through
   the pinned public Wayback Machine Downloader and serial persistent client.
4. Review the resulting archive file set and complete sources, uncheck unwanted
   captures and **Approve publication & queue indexing**. This second approval
   binds to the actual source hashes and selected subset, which can differ from
   the initial samples Luna assessed. Import includes AI enrichment by default,
   with a separate $2 limit shown before publication approval.
5. Publication makes one fast-forward archive commit, including
   `crawl-manifests/<batch-id>.json`. Import waits for every other unfinished,
   unsuspended Job in `eqarchives-es`, including pending/retrying Jobs with
   `active=0`. The controller cannot patch, suspend or delete Jobs.

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
extraction receives no synthetic capture header and requires verbatim source
evidence. Sources exceeding the 900 KB serialized enrichment bound remain
pending rather than being truncated.

Each approved publication batch has a separate cumulative $2 enrichment cap.
Reservations use conservative long-context rates and precede calls; ambiguous
failures retain reservations. Verified results are cached by source/prompt/model
signature in a writable `enrichment/` subdirectory on the PVC. The approved
sources/manifests remain read-only in import Jobs. Completed results are reused
on retry, while existing document IDs incur no model calls. Failed enrichment
or an exhausted budget leaves that document pending instead of silently creating
an unenriched entry. There is no index/mapping creation, RabbitMQ or broad scan.
Kubernetes retries twice; terminal failures remain visible for
operator investigation, without deleting/replacing an existing Job.

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
the preceding immutable curation digest and retains the PVC. Unchanged deployment
inputs preserve the Deployment generation/pod UID.

```bash
docker build -f elastic-indexer-stack/curation/Dockerfile \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" -t eqarchives-curation:local .
BROWSER_TEST_PYTHON=/path/to/playwright-venv/bin/python \
  bash scripts/smoke-curation.sh eqarchives-curation:local
kubectl kustomize elastic-indexer-stack/curation/k8s >/dev/null
```

The image build runs Python 3.13 API/state/scope/source/campaign/publication/import
pytest. CI also uses real Chromium at 320/390/1280 px, delayed source responses,
durable decisions and final publication approval in isolated fixtures with no
paid calls or real archive writes. Real TCP tests reject non-LAN peers and
forwarded-header spoofs. Deployment checks the live queue/build and hashes
existing unfinished Job specs before/after; it never saves their credential-
bearing JSON to disk.
