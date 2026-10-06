#!/usr/bin/env bash
set -euo pipefail
image=${1:?Usage: deploy-curation.sh IMMUTABLE_CURATION_IMAGE}
if [[ ! "$image" =~ ^dbsanfte/frontend@sha256:[a-f0-9]{64}$ ]]; then
  echo 'Curation deployment requires an immutable dbsanfte/frontend artifact.' >&2
  exit 1
fi
: "${ARCHIVE_CRAWLER_OPENAI_API_KEY:?Set ARCHIVE_CRAWLER_OPENAI_API_KEY}"
: "${ARCHIVE_PUBLISH_SSH_KEY:?Set ARCHIVE_PUBLISH_SSH_KEY}"
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
kubectl=(sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml)
curation_work=$(mktemp -d)
trap 'rm -rf "$curation_work"' EXIT
umask 077

# Snapshot existing unfinished ingestion Jobs. Neither this script nor the
# controller has a reason to alter them or their source checkouts.
"${kubectl[@]}" -n eqarchives-es get jobs -o json |
  python3 "$repo_dir/scripts/indexer-job-guard.py" > "$curation_work/jobs-before.json"
cp "$repo_dir"/elastic-indexer-stack/curation/k8s/{curation.yaml,kustomization.yaml} "$curation_work/"
checksum=$(python3 - <<'PY'
import hashlib,json,os
print(hashlib.sha256(json.dumps({key:os.environ[key] for key in ('ARCHIVE_CRAWLER_OPENAI_API_KEY','ARCHIVE_PUBLISH_SSH_KEY')},sort_keys=True).encode()).hexdigest())
PY
)
cat >> "$curation_work/kustomization.yaml" <<EOF
images:
  - name: eqarchives-curation
    newName: dbsanfte/frontend
    digest: ${image#*@}
patches:
  - target:
      kind: Deployment
      name: eqarchives-curation
    patch: |-
      - op: add
        path: /spec/template/metadata/annotations
        value:
          checksum/curation-secrets: "$checksum"
      - op: replace
        path: /spec/template/spec/containers/0/env/1/value
        value: "$image"
EOF
"${kubectl[@]}" kustomize "$curation_work" > "$curation_work/resources.yaml"
"${kubectl[@]}" apply --dry-run=server -f "$curation_work/resources.yaml" >/dev/null
elastic_ip=$("${kubectl[@]}" -n eqarchives-es get service elasticsearch -o jsonpath='{.spec.clusterIP}')
export CURATION_ES_URL="http://$elastic_ip:9200"
"${kubectl[@]}" -n eqarchives-es get secret elastic-password-secret eqarchives-capture-indexer-secrets --ignore-not-found -o json |
  python3 "$repo_dir/scripts/curation-secrets.py" |
  "${kubectl[@]}" apply --server-side --force-conflicts --field-manager=curation-cicd -f - >/dev/null
"${kubectl[@]}" apply -f "$curation_work/resources.yaml"
"${kubectl[@]}" -n eqarchives-es rollout status deployment/eqarchives-curation --timeout=300s
"${kubectl[@]}" -n eqarchives-es get jobs -o json |
  python3 "$repo_dir/scripts/indexer-job-guard.py" --before "$curation_work/jobs-before.json"
python3 "$repo_dir/scripts/check-curation.py" --image "$image"
echo "Deployed and verified intranet curation $image"
