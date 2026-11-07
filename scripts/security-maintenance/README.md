# Search security infrastructure maintenance

Normal frontend CI deploys the bounded search/embedding adapter, escaped result
rendering, credential-free hardened static pods, network policies and the
ClusterIP-only Elasticsearch REST Service. It does not restart Elasticsearch,
indexing Jobs or k3s.

## Ingress

`traefik.yaml` is a HelmChartConfig override for the installed k3s chart. It pins
Traefik 2.11.61, keeps the existing chart/ports/provider configuration, adds
request/response/idle deadlines and uses two replicas. The installed chart must
retain zero-unavailable rolling updates and RBAC for both `traefik.io` and
`traefik.containo.us`; both CRD groups must already exist. Render it with the
installed chart and existing values before applying. CI uses the same image in
`scripts/routing-test.env` for actual HTTP routing and shutdown checks.

After the protected PR is merged and deployed:

```sh
sudo kubectl -n kube-system apply -f scripts/security-maintenance/traefik.yaml
sudo kubectl -n kube-system rollout status deployment/traefik --timeout=300s
```

Check the Helm controller upgrade actually completed before treating an unchanged
Deployment as updated. Verify the running image/version, all search API smoke
checks, curation health and any unrelated hosts served by this shared ingress.
Retain the previous Helm values/configuration privately for emergency recovery.
Do not edit k3s-generated manifests or weaken RBAC/branch protection.

## Database version and TLS: separate maintenance window

The pinned target in `runtime.env` is Elasticsearch 8.19.24. Verify a recent
successful S3 snapshot and deprecation/cluster health before upgrading; retain
all existing Jobs, immutable images, ConfigMaps, prepared sources and checkpoints.
The single-node database cannot change its binary or enable native REST/transport
TLS without restarting. The operator's instruction to preserve the active full
reindex takes precedence: do not apply this pin or suspend that Job during normal
frontend delivery. Obtain a maintenance decision once the nondisruptive work is
complete. Ordinary image rollback cannot downgrade an upgraded data directory;
recovery requires a compatible backup/snapshot.

Before a maintenance window, run the isolated compatibility check:

```sh
python3 scripts/security-maintenance/check-elasticsearch-upgrade.py
# Optionally use a private mapping-only JSON export from the current archive:
python3 scripts/security-maintenance/check-elasticsearch-upgrade.py \
  --mapping-file /private/path/archive-mapping.json
```

This starts disposable, resource-limited Docker nodes on loopback, creates one
synthetic record with a normalized nested vector, upgrades its private volume
from 8.17.0 to the pinned target, and verifies exact source/checkpoint identity
and 768-dimensional kNN before/after. It never accepts production credentials or
addresses and deletes only its own temporary containers/volume. It is a mapping
and storage compatibility check, not a full backup restore rehearsal.

The live handoff must additionally provision a private CA and service certificates,
enable native HTTP/transport TLS, and update every database client to verify that
CA (never `verify_certs=false`). Include the search API, curation/import Jobs,
indexer manager/reindex Jobs, schema/credential provisioners and private operator
helpers. Create a numbered indexer continuation from the retained immutable
image/source/checkpoint, with the new TLS endpoint/CA only; do not change passage
or embedding versions, rerun source preparation, discard caches, delete old Jobs,
or pay for enrichment. Probe one exact remaining ID, then resume and confirm
acknowledged writes advance. Follow the Jobs and reindex-recovery runbooks.

The old k3s/containerd versions also require a separately planned supported
upgrade path. Respect Kubernetes version skew and check packaged Traefik/CRD
migrations, device-plugin behavior, storage/mount requirements and unrelated
cluster applications. A current kernel alone does not update the container
runtime. Keep this control-plane maintenance out of ordinary portal deployment.
