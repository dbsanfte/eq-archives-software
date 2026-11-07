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
container_id=$(docker run --detach --publish 127.0.0.1::8080 \
  --cap-drop ALL --security-opt no-new-privileges --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --env SEARCH_API_URL=http://127.0.0.1:1234 "$image")
[[ "$(docker exec "$container_id" id -u)" == 10001 ]]
address=$(docker port "$container_id" 8080/tcp)
bash "$(dirname "${BASH_SOURCE[0]}")/wait-http.sh" "http://$address/healthz" | grep -qx 'ok'
curl --fail --silent --show-error "http://$address/" > "$work_dir/index.html"
grep -q 'id="root"' "$work_dir/index.html"
asset=$(python3 -c 'import re,sys; print(re.search(r"src=\"([^\"]+\.js)\"", open(sys.argv[1]).read()).group(1))' "$work_dir/index.html")
curl --fail --silent --show-error "http://$address$asset" > "$work_dir/app.js"
if [[ -n "${EXPECTED_GIT_SHA:-}" ]] && ! grep -Fq "$EXPECTED_GIT_SHA" "$work_dir/app.js"; then
  echo 'Browser bundle does not contain the expected Git revision.' >&2
  exit 1
fi
if grep -Eq 'ci-password-never-a-real-secret|ci-placeholder|elasticsearch_password' "$work_dir/app.js"; then
  echo 'Browser bundle contains server credential configuration.' >&2
  exit 1
fi
echo 'Frontend health, HTML, JavaScript, build revision, and credential isolation checks passed.'
python3 "$(dirname "${BASH_SOURCE[0]}")/check-static-assets.py" "http://$address"
if [[ -n "${BROWSER_TEST_PYTHON:-}" ]]; then
  FRONTEND_BASE_URL="http://$address" "$BROWSER_TEST_PYTHON" \
    "$(dirname "${BASH_SOURCE[0]}")/test-frontend-browser.py"
fi
