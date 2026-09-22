#!/usr/bin/env bash
set -euo pipefail

image=${1:?Usage: smoke-embeddings.sh FRONTEND_IMAGE}
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
embedding_dir="$repo_dir/elastic-indexer-stack/k8s-manifests/embeddings"
set -a
source "$embedding_dir/runtime.env"
set +a
work_dir=$(mktemp -d)
network="eqarchives-embedding-smoke-$$"
embedding_id=''
frontend_id=''
cleanup() {
  if [[ -n "$frontend_id" ]]; then docker rm -f "$frontend_id" >/dev/null; fi
  if [[ -n "$embedding_id" ]]; then docker rm -f "$embedding_id" >/dev/null; fi
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$work_dir"
}
trap cleanup EXIT
mkdir -p "$work_dir/models" "$work_dir/cache" "$work_dir/secrets"
printf '%s' 'ci-readonly' > "$work_dir/secrets/es_readonly_username"
printf '%s' 'ci-password-never-a-real-secret' > "$work_dir/secrets/es_readonly_password"
printf '%s' 'ci-embedding-api-key' > "$work_dir/secrets/openai_api_key"

docker run --rm --user "$(id -u):$(id -g)" --cap-drop ALL \
  --security-opt no-new-privileges --read-only \
  --env-file "$embedding_dir/runtime.env" --entrypoint /bin/sh \
  --mount "type=bind,source=$embedding_dir,target=/bootstrap,readonly" \
  --mount "type=bind,source=$work_dir/models,target=/models" \
  "$LLAMA_IMAGE" /bootstrap/download-model.sh
# Verify that the same bootstrap can reuse the verified model without network.
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --env-file "$embedding_dir/runtime.env" --entrypoint /bin/sh \
  --mount "type=bind,source=$embedding_dir,target=/bootstrap,readonly" \
  --mount "type=bind,source=$work_dir/models,target=/models,readonly" \
  "$LLAMA_IMAGE" /bootstrap/download-model.sh

docker network create "$network" >/dev/null
embedding_id=$(docker run --detach --network "$network" --network-alias nomic \
  --publish 127.0.0.1::8080 --user "$(id -u):$(id -g)" \
  --cap-drop ALL --security-opt no-new-privileges --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m --memory 1g \
  --env-file "$embedding_dir/runtime.env" \
  --env LLAMA_DEVICE=none --env LLAMA_GPU_LAYERS=0 --env LLAMA_THREADS=2 \
  --mount "type=bind,source=$embedding_dir,target=/bootstrap,readonly" \
  --mount "type=bind,source=$work_dir/models,target=/models,readonly" \
  --mount "type=bind,source=$work_dir/cache,target=/cache" \
  --mount "type=bind,source=$work_dir/secrets,target=/run/secrets,readonly" \
  --entrypoint /bin/sh "$LLAMA_IMAGE" /bootstrap/start-server.sh)
embedding_address=$(docker port "$embedding_id" 8080/tcp)
curl --fail --silent --show-error --retry 30 --retry-all-errors --retry-delay 1 \
  --max-time 5 "http://$embedding_address/health" >/dev/null
status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  -H 'Content-Type: application/json' --data '{"input":"test"}' \
  "http://$embedding_address/v1/embeddings")
if [[ "$status" != 401 ]]; then
  echo "Unauthenticated embedding request should return 401; received $status." >&2
  exit 1
fi

frontend_id=$(docker run --detach --network "$network" --publish 127.0.0.1::80 \
  --mount "type=bind,source=$work_dir/secrets,target=/run/secrets,readonly" \
  --env ELASTICSEARCH_URL=http://127.0.0.1:9200 \
  --env ELASTICSEARCH_INDEX=eq-archive --env OPENAI_URL=http://nomic:8080 "$image")
frontend_address=$(docker port "$frontend_id" 80/tcp)
curl --fail --silent --show-error --retry 15 --retry-connrefused --retry-delay 1 \
  --max-time 5 "http://$frontend_address/healthz" >/dev/null
python3 "$repo_dir/scripts/check-embeddings.py" \
  "http://$frontend_address/openai/v1/embeddings" --model "$MODEL_ALIAS"
if docker exec "$embedding_id" /bin/sh /bootstrap/check-gpu.sh; then
  echo 'GPU verification incorrectly accepted the CPU-only CI server.' >&2
  exit 1
fi

# An oversized query must fail cleanly and leave the service usable.
python3 -c 'import json,os; print(json.dumps({"model":os.environ["MODEL_ALIAS"],"input":["word "*600]}))' > "$work_dir/oversized.json"
status=$(curl --silent --show-error --output "$work_dir/oversized-response.json" --write-out '%{http_code}' \
  -H 'Content-Type: application/json' --data-binary "@$work_dir/oversized.json" \
  "http://$frontend_address/openai/v1/embeddings")
if [[ "$status" != 500 ]]; then
  echo "The pinned runtime should reject oversized input with HTTP 500; received $status." >&2
  exit 1
fi
python3 - "$work_dir/oversized-response.json" <<'PY'
import json, sys
message = json.load(open(sys.argv[1])).get("error", {}).get("message", "")
assert "input (602 tokens) is too large to process" in message, "Unexpected oversized-input error"
PY
python3 "$repo_dir/scripts/check-embeddings.py" \
  "http://$frontend_address/openai/v1/embeddings" --model "$MODEL_ALIAS"
echo 'Model integrity, cache reuse, authentication, frontend proxy, dimensions, and oversized-input checks passed.'
