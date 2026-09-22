#!/usr/bin/env bash
set -euo pipefail

image=${1:?Usage: smoke-frontend.sh IMAGE}
work_dir=$(mktemp -d)
container_id=''
cleanup() {
  if [[ -n "$container_id" ]]; then docker rm -f "$container_id" >/dev/null; fi
  rm -rf "$work_dir"
}
trap cleanup EXIT
printf '%s' 'ci-readonly' > "$work_dir/es_readonly_username"
printf '%s' 'ci-password-never-a-real-secret' > "$work_dir/es_readonly_password"
printf '%s' 'ci-placeholder' > "$work_dir/openai_api_key"
container_id=$(docker run --detach --publish 127.0.0.1::80 \
  --mount "type=bind,source=$work_dir,target=/run/secrets,readonly" \
  --env ELASTICSEARCH_URL=http://127.0.0.1:9200 \
  --env ELASTICSEARCH_INDEX=eq-archive \
  --env OPENAI_URL=http://127.0.0.1:1234 "$image")
address=$(docker port "$container_id" 80/tcp)
curl --fail --silent --show-error --retry 15 --retry-connrefused --retry-delay 1 \
  --max-time 5 "http://$address/healthz" | grep -qx 'ok'
curl --fail --silent --show-error "http://$address/" > "$work_dir/index.html"
grep -q 'id="root"' "$work_dir/index.html"
asset=$(python3 -c 'import re,sys; print(re.search(r"src=\"([^\"]+\.js)\"", open(sys.argv[1]).read()).group(1))' "$work_dir/index.html")
curl --fail --silent --show-error "http://$address$asset" > "$work_dir/app.js"
if grep -Eq 'ci-password-never-a-real-secret|ci-placeholder|elasticsearch_password' "$work_dir/app.js"; then
  echo 'Browser bundle contains server credential configuration.' >&2
  exit 1
fi
echo 'Frontend health, HTML, JavaScript, and credential isolation checks passed.'
