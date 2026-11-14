#!/usr/bin/env bash
# On the RTX host: install.sh IMMUTABLE_LOCAL_IMAGE_ID EXACT_GIT_SHA LAN_ADDRESS
set -euo pipefail
test "$(id -u)" = 0
test "$#" = 3
cd "$(dirname "$0")"
test -s /etc/eqarchives-embeddings/openai_api_key
test -s /etc/eqarchives-reranker/api_key
install -d -m 755 /etc/eqarchives-cuda-batch /opt/eqarchives-cuda-batch-service /var/lib/eqarchives-cuda-batch/models
python3 - "$1" "$2" "$3" <<'PY'
import json, os, pathlib, subprocess, sys, urllib.request
from backend import PINS, verified
from service import settings
image, revision, address = sys.argv[1:]
config = pathlib.Path('/etc/eqarchives-cuda-batch/config.json')
pending = config.with_suffix('.pending')
pending.write_text(json.dumps(dict(image=image, revision=revision, bind_address=address)))
settings(pending)
metadata = json.loads(subprocess.check_output(['docker','image','inspect',image]))[0]
if metadata['Config'].get('Labels',{}).get('org.opencontainers.image.revision') != revision:
    raise ValueError('Image/source revision mismatch')
root = pathlib.Path('/var/lib/eqarchives-cuda-batch/models')
sources = dict(embed=pathlib.Path('/var/lib/eqarchives-embeddings/models'), rerank=pathlib.Path('/var/lib/eqarchives-reranker/models'))
urls = {
 'embed': 'https://huggingface.co/google/embeddinggemma-2/resolve/f9972ac22e696a105c482202646979e33e4b5a1e/tokenizer.json',
 'rerank': 'https://huggingface.co/Qwen/Qwen3-Reranker-0.6B/resolve/e61197ed45024b0ed8a2d74b80b4d909f1255473/tokenizer.json',
}
for kind, (filename, digest, tokenizer, tok_digest) in PINS.items():
    target = root/filename
    if not target.exists():
        os.link(verified(sources[kind]/filename, digest), target)
    verified(target,digest)
    target = root/tokenizer
    if not target.exists():
        temporary = target.with_suffix('.download')
        with urllib.request.urlopen(urls[kind],timeout=120) as reply:
            data = reply.read(40*1024*1024+1)
        if len(data)>40*1024*1024: raise ValueError('Tokenizer size bound')
        temporary.write_bytes(data)
        verified(temporary,tok_digest).replace(target)
    verified(target,tok_digest)
pending.replace(config)
PY
install -m 644 service.py /opt/eqarchives-cuda-batch-service/
install -m 644 eqarchives-cuda-batch.service eqarchives-cuda-batch-health.service eqarchives-cuda-batch-health.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable eqarchives-cuda-batch.service eqarchives-cuda-batch-health.timer
systemctl restart eqarchives-cuda-batch.service
systemctl start eqarchives-cuda-batch-health.timer
