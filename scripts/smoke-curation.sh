#!/usr/bin/env bash
set -euo pipefail
image=${1:?Usage: smoke-curation.sh CURATION_IMAGE}
: "${BROWSER_TEST_PYTHON:?Set BROWSER_TEST_PYTHON}"
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
curation_suffix="$$"
curation_network="curation-test-$curation_suffix"
curation_container="curation-test-$curation_suffix"
cleanup() {
  docker rm -f "$curation_container" >/dev/null 2>&1 || true
  docker network rm "$curation_network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create --subnet 192.168.240.0/24 "$curation_network" >/dev/null
docker run -d --name "$curation_container" --network "$curation_network" --ip 192.168.240.2 \
  --read-only --tmpfs /tmp:rw,uid=10001,gid=10001,mode=700 --tmpfs /data:rw,uid=10001,gid=10001,mode=700 \
  --cap-drop ALL --security-opt no-new-privileges \
  -e LAN_BIND_IP=192.168.240.2 "$image" python /app/curation/browser_fixture.py >/dev/null
bash "$repo_dir/scripts/wait-http.sh" http://192.168.240.2:8090/healthz
"$BROWSER_TEST_PYTHON" "$repo_dir/scripts/test-curation-browser.py" http://192.168.240.2:8090
# Real TCP peer checks against the built service, including spoofed headers.
docker exec -i "$curation_container" python - <<'PY'
import http.client
for path in ('/','/api/queue','/assets/review.js','/healthz'):
    connection=http.client.HTTPConnection('192.168.240.2',8090,source_address=('127.0.0.1',0),timeout=5)
    connection.request('GET',path,headers={'X-Forwarded-For':'192.168.50.20','X-Real-IP':'192.168.50.20'})
    response=connection.getresponse()
    assert response.status==403,(path,response.status)
    assert b'Intranet access only' in response.read()
    connection.close()
print('Non-LAN TCP peers and forwarded-header spoofs rejected on UI/API/assets/health')
PY
