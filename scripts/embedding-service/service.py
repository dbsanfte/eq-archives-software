"""Pinned, private-LAN CUDA embedding service for a standalone Linux GPU host."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import urllib.request

NAME = 'eqarchives-nomic-cuda'
ROOT = Path('/opt/eqarchives-embeddings')
KEY = Path('/etc/eqarchives-embeddings/openai_api_key')
MODEL_ROOT = Path('/var/lib/eqarchives-embeddings/models')


def settings(root=ROOT):
    config = {}
    for name in ('runtime.env', 'cuda.env'):
        for line in (root / name).read_text().splitlines():
            if line.strip() and not line.startswith('#'):
                key, value = line.split('=', 1)
                config[key] = value
    address = ipaddress.ip_address(config['BIND_ADDRESS'])
    if address not in ipaddress.ip_network('192.168.0.0/16'):
        raise ValueError('Bind to the private wired LAN address')
    if not 1024 <= int(config['BIND_PORT']) <= 65535:
        raise ValueError('Invalid service port')
    if config['CUDA_SLOTS'] != '64' or config['CUDA_COMPUTE_BATCH'] != '512':
        raise ValueError('Use the benchmarked 64-slot, 512-token compute profile')
    if '@sha256:' not in config['CUDA_IMAGE']:
        raise ValueError('An immutable CUDA image pin is required')
    return config


def command(config):
    slots = int(config['CUDA_SLOTS'])
    return ['docker', 'run', '--rm', '--name', NAME,
            '--label', 'eqarchives.managed=cuda-embeddings', '--device', 'nvidia.com/gpu=all',
            '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m',
            '--publish', f"{config['BIND_ADDRESS']}:{config['BIND_PORT']}:8080",
            '--mount', f'type=bind,source={MODEL_ROOT},target=/models,readonly',
            '--mount', f'type=bind,source={KEY},target=/run/secrets/openai_api_key,readonly',
            '--mount', 'type=bind,source=/var/lib/eqarchives-embeddings/cache,target=/cache',
            '--env', 'XDG_CACHE_HOME=/cache', '--env', 'CUDA_CACHE_PATH=/cache/nvidia',
            '--entrypoint', '/app/llama-server', config['CUDA_IMAGE'],
            '--model', f"/models/{config['MODEL_FILE']}", '--alias', config['MODEL_ALIAS'],
            '--host', '0.0.0.0', '--port', '8080', '--embedding', '--pooling', 'mean',
            '--ctx-size', str(slots * 512), '--parallel', str(slots), '--kv-unified-per-slot', '512',
            '--batch-size', config['CUDA_COMPUTE_BATCH'], '--ubatch-size', config['CUDA_COMPUTE_BATCH'],
            '--threads', '8', '--threads-batch', '8', '--device', 'CUDA0', '--gpu-layers', '99',
            '--flash-attn', 'on', '--no-cache-prompt', '--cache-ram', '0',
            '--api-key-file', '/run/secrets/openai_api_key', '--no-webui', '--log-verbosity', '1']


def healthy(config, opener=urllib.request.urlopen):
    try:
        body = json.dumps({'model': config['MODEL_ALIAS'], 'input': ['search_document: EverQuest guild'],
                           'encoding_format': 'float'}).encode()
        request = urllib.request.Request(
            f"http://{config['BIND_ADDRESS']}:{config['BIND_PORT']}/v1/embeddings", data=body,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY.read_text().strip()})
        with opener(request, timeout=15) as response:
            from benchmark import vectors
            vectors(json.load(response), 1, config['MODEL_ALIAS'])
        return True
    except Exception:
        # Do not print upstream bodies or credential-bearing exception strings.
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'health'))
    args = parser.parse_args()
    config = settings()
    if args.action == 'start':
        import hashlib
        model = MODEL_ROOT / config['MODEL_FILE']
        with model.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != config['MODEL_SHA256']:
                raise ValueError('Model checksum mismatch')
        if not KEY.read_text().strip():
            raise ValueError('Embedding API key is empty')
        subprocess.run(['docker', 'rm', '-f', NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.execvp('docker', command(config))
    failures = Path('/run/eqarchives-embeddings/health-failures')
    if not failures.parent.is_dir():
        print('Embedding service has not started; inference health unavailable', flush=True)
        return 1
    if healthy(config):
        failures.write_text('0')
        return 0
    count = int(failures.read_text()) + 1 if failures.exists() else 1
    failures.write_text(str(count))
    print(f'Embedding health check failed ({count}/3); upstream details omitted', flush=True)
    if count >= 3:
        subprocess.run(['systemctl', 'try-restart', 'eqarchives-embeddings.service'], check=True)
        failures.write_text('0')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
