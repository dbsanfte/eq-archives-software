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
docker save eqarchives-indexer:blueworld-recovery | sudo -n k3s ctr images import -
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
