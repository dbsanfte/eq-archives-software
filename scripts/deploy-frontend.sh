#!/usr/bin/env bash
set -euo pipefail

# Run on eqvm, where the existing runner can access the local k3s API.
image=${1:?Usage: deploy-frontend.sh dbsanfte/frontend@sha256:DIGEST}
if [[ ! "$image" =~ ^dbsanfte/frontend@sha256:[a-f0-9]{64}$ ]]; then
  echo 'Deployment requires an immutable dbsanfte/frontend image digest.' >&2
  exit 1
fi
: "${FRONTEND_ES_USERNAME:?Set FRONTEND_ES_USERNAME}"
: "${FRONTEND_ES_PASSWORD:?Set FRONTEND_ES_PASSWORD}"
: "${FRONTEND_OPENAI_API_KEY:?Set FRONTEND_OPENAI_API_KEY}"
: "${DOCKERHUB_USERNAME:?Set DOCKERHUB_USERNAME}"
: "${DOCKERHUB_TOKEN:?Set DOCKERHUB_TOKEN}"

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
kubectl=(sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml)
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
umask 077

# Render all public resources and validate them before changing the cluster.
cp "$repo_dir"/elastic-indexer-stack/k8s-manifests/{01-frontend.yaml,kustomization.yaml} "$work_dir/"
secret_checksum=$(python3 - <<'PY'
import hashlib, json, os
values = {name: os.environ[name] for name in (
    "FRONTEND_ES_USERNAME", "FRONTEND_ES_PASSWORD", "FRONTEND_OPENAI_API_KEY"
)}
print(hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest())
PY
)
cat >> "$work_dir/kustomization.yaml" <<EOF
images:
  - name: dbsanfte/frontend
    newName: dbsanfte/frontend
    digest: ${image#*@}
patches:
  - target:
      kind: Deployment
      name: search-eqarchives
    patch: |-
      - op: add
        path: /spec/template/metadata/annotations
        value:
          checksum/frontend-secrets: "$secret_checksum"
EOF
"${kubectl[@]}" kustomize "$work_dir" > "$work_dir/frontend.yaml"
"${kubectl[@]}" apply --dry-run=server -f "$work_dir/frontend.yaml" >/dev/null
"${kubectl[@]}" create namespace eqarchives-es --dry-run=client -o yaml |
  "${kubectl[@]}" apply -f -

# Keep secret values off command lines, disk, logs, and last-applied annotations.
python3 "$repo_dir/scripts/frontend-secrets.py" |
  "${kubectl[@]}" apply --server-side --force-conflicts \
    --field-manager=frontend-cicd -f - >/dev/null

"${kubectl[@]}" apply -f "$work_dir/frontend.yaml"
"${kubectl[@]}" -n eqarchives-es rollout status deployment/search-eqarchives --timeout=300s

# Verify both NGINX and the authenticated Elasticsearch proxy through Traefik.
# Resolve directly to this VM while still checking the real TLS certificate.
curl --fail --silent --show-error --retry 5 --retry-all-errors --retry-delay 2 \
  --max-time 30 --resolve search.eqarchives.org:443:127.0.0.1 \
  https://search.eqarchives.org/healthz | grep -qx 'ok'
curl --fail --silent --show-error --retry 5 --retry-all-errors --retry-delay 2 \
  --max-time 30 --resolve search.eqarchives.org:443:127.0.0.1 \
  -H 'Content-Type: application/json' \
  --data '{"size":0,"query":{"match_all":{}}}' \
  https://search.eqarchives.org/elasticsearch/eq-archive/_search |
  python3 -c 'import json,sys; result=json.load(sys.stdin); assert "hits" in result and not result.get("error"), "Elasticsearch smoke check failed"'
echo "Deployed and verified $image"
