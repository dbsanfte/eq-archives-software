#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) != 0 ]]; then
  echo 'Run with sudo; this installs only the independent reranker pilot.' >&2
  exit 1
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
model_source=${1:?Pass the converted, hash-verified GGUF path}
key_source=${2:?Pass the private pilot API-key file path}
python3 - "$source_dir" "$model_source" <<'PY'
import sys,hashlib
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from service import settings
config=settings(Path(sys.argv[1]));digest=hashlib.sha256()
with Path(sys.argv[2]).open('rb') as stream:
    while chunk:=stream.read(1024*1024):digest.update(chunk)
if digest.hexdigest()!=config['MODEL_SHA256']:raise SystemExit('Converted model differs from the tested pin')
PY
test -s "$key_source"
install -d -m 0755 /opt/eqarchives-reranker /var/lib/eqarchives-reranker/models
install -d -m 0750 -o root -g 1000 /etc/eqarchives-reranker
install -m 0644 "$source_dir/service.py" "$source_dir/runtime.env" /opt/eqarchives-reranker/
if ! cmp -s -- "$model_source" /var/lib/eqarchives-reranker/models/qwen3-reranker-0.6b-q8_0.gguf; then
  install -m 0644 -- "$model_source" /var/lib/eqarchives-reranker/models/qwen3-reranker-0.6b-q8_0.gguf
fi
install -m 0600 -o 1000 -g 1000 -- "$key_source" /etc/eqarchives-reranker/api_key
install -m 0644 "$source_dir/eqarchives-reranker-pilot.service" /etc/systemd/system/
python3 /opt/eqarchives-reranker/service.py validate
systemctl daemon-reload
systemctl enable --now eqarchives-reranker-pilot.service
