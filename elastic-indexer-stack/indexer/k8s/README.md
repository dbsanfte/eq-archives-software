# BlueWorld newsgroup recovery indexing

`blueworld-index-job.yaml` is a one-off Job for the 11,914 recovered
`alt.games.everquest` articles. It is deliberately outside the frontend
Kustomization. It reads the existing archive checkout on eqvm at
`/mnt/samsung/eq-archives` through a read-only `hostPath` mount and writes to
the existing `eq-archive` Elasticsearch index. It uses the cluster's Nomic
embedding service with the document prefix and 768-dimensional vectors.
`SKIP_LLM_ENRICHMENT=true` prevents summaries, tags and other LLM work.
The Job leaves the existing Elasticsearch index template and mappings untouched.

The job verifies all files against the recovery manifest before indexing. It
uses each archive-relative path as the Elasticsearch document ID, skips IDs
already present, and creates new IDs only. Rerunning it resumes after an
interruption. It exits nonzero on a source, embedding or indexing failure.
The source URL in each document points to the matching path on the archive's
`mailing-lists` GitHub Pages branch; publish that branch first.

On eqvm, after `eq-archives` has been updated to the committed recovery and
the raw Pages URLs return HTTP 200:

```bash
docker build --tag eqarchives-indexer:blueworld-recovery elastic-indexer-stack/indexer
docker save eqarchives-indexer:blueworld-recovery | sudo -n k3s ctr -n k8s.io images import -
sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml apply \
  -f elastic-indexer-stack/indexer/k8s/blueworld-index-job.yaml
sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n eqarchives-es \
  logs -f job/index-blueworld-pre2000
sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n eqarchives-es \
  wait --for=condition=complete job/index-blueworld-pre2000 --timeout=24h
```

If the Python code changes after an attempt, delete the completed or failed
Job, rebuild/import the image, and apply the manifest again. Existing document
IDs remain, so the next run continues with missing IDs. The Job uses the
existing `elastic-password-secret` for writes and
`search-eqarchives-secrets` for the Nomic API key; no credential is included
in the image or manifest.

## Broad text reindex

`reindex-text-job.yaml` repairs existing `file_type=text` records and rebuilds
their document embeddings with the pinned Nomic tokenizer. It is outside the
frontend Kustomization. No index is deleted, no LLM enrichment runs, and existing
summaries, tags, estimated dates, OCR, provenance and unknown fields are retained.
Only `text_full`, `text`, their extraction/chunking version fields and
`last_indexed` are updated. The two version fields have additive keyword mappings.
The exact ES ID and stored archive ID must agree, and source paths/symlinks must
stay inside the checkout. Optimistic concurrency leaves concurrent writes intact.

Each completed record is checkpointed by its version fields. Live `search_after`
paging uses the unique archive ID without holding old ES segments for the whole
multi-day run. A retry scans remaining versions; failures are counted, upstream
payloads are not logged, and the Job exits nonzero if records failed. This is a
live-index scan, not a snapshot. Keep other ingestion jobs separate during it.

Website text includes the reconstructed `Page URL` and, when different, its
`Alternate Page URL` in the stored Markdown header. The alternate drops a final
`/index.html`, which the downloader can add locally for extensionless pages; it
is a candidate source link, not a claim that every `index.html` was synthetic.
The website extraction checkpoint advances for this header repair. Completed
newsgroups and mailing lists retain their previous extraction checkpoint and
are skipped on resume when their chunking version is current. Previously
completed websites receive the header update; all other completed text is kept.

The dedicated checkout at `/mnt/samsung/eq-archives-html-reindex` avoids changing
the ingestion checkout's sparse configuration. Prepare it once on eqvm as UID
1000; use a committed archive revision and keep its SHA with the run record:

```bash
sudo -n mkdir -p /mnt/samsung/eq-archives-html-reindex
sudo -n chown dbsanfte:dbsanfte /mnt/samsung/eq-archives-html-reindex
git clone --filter=blob:none --depth=1 --no-checkout \
  https://github.com/dbsanfte/eq-archives.git /mnt/samsung/eq-archives-html-reindex
git -C /mnt/samsung/eq-archives-html-reindex sparse-checkout set --cone websites/www.fohguild.org
git -C /mnt/samsung/eq-archives-html-reindex checkout
git -C /mnt/samsung/eq-archives-html-reindex rev-parse HEAD
```

The broad Job's init container expands this checkout to websites, newsgroups and
mailing lists at that same revision. It uses the image's Git to bulk-prefetch
blobs under 4 MiB with `fetch --refetch` before checkout; remaining larger files
are fetched as needed without truncation. A per-revision cache marker avoids
repeating the bulk transfer on retry. The init container permits 8 GiB for the
large Git index and fetch, while the serial indexing process is limited to 2 GiB.
Allow space and time for archive downloads;
it never modifies the original archive repository or ingester checkout. Its main
container reads sources only. Missing files are counted and their existing text
is rechunked without marking extraction repaired. A source absent from this
archive branch needs the appropriate checkout before its extraction can be fixed.

After the protected PR passes and merges, build/import an image with a unique
commit tag. Replace both `deploy-reindex-image` placeholders in the manifest with
that tag before applying; keep the rendered manifest out of Git. Run a probe with
`--id '<exact ES ID>'` and without the source-preparation init container first,
using the same image, mount and secret references. Verify the document and its
metadata through the public reader before launching the broad Job.

```bash
docker build --tag eqarchives-indexer:reindex-<commit-sha> elastic-indexer-stack/indexer
docker save eqarchives-indexer:reindex-<commit-sha> | sudo -n k3s ctr -n k8s.io images import -
# Set both image tags in a temporary copy of reindex-text-job.yaml.
sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml apply -f /tmp/reindex-text-job.yaml
sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n eqarchives-es \
  logs -f job/reindex-text-html-20261005 -c indexer
```

The workload is serial, throttled by `EMBEDDING_REQUEST_PAUSE=0.2`, and uses the
existing 768-dimensional Nomic model and credentials. Logs report processed,
updated, unchanged, unavailable-source and failed counts. Delete/recreate only
this Job to resume a failed attempt; retain the dedicated source cache. Recheck
public search/reader/MCP health while it runs. A triggered Job is not a completed
reindex; report its actual phase and progress separately.
