"""Independent loopback-only Qwen reranker. Never restarts embedding services."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import urllib.request

NAME = 'eqarchives-reranker-pilot'
ROOT = Path('/opt/eqarchives-reranker')
MODELS = Path('/var/lib/eqarchives-reranker/models')
KEY = Path('/etc/eqarchives-reranker/api_key')


def settings(root=ROOT):
    result = {}
    for line in (root / 'runtime.env').read_text().splitlines():
        if line.strip() and not line.startswith('#'):
            name, value = line.split('=', 1)
            if name in result:
                raise ValueError('Duplicate reranker setting')
            result[name] = value
    if result['BIND_ADDRESS'] != '127.0.0.1' or not 1024 <= int(result['BIND_PORT']) <= 65535:
        raise ValueError('The experimental reranker must bind to loopback')
    if not re.fullmatch(r'ghcr.io/ggml-org/llama.cpp@sha256:[0-9a-f]{64}', result['LLAMA_IMAGE']):
        raise ValueError('Use a pinned llama.cpp image')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+\.gguf', result['MODEL_FILE']):
        raise ValueError('Use a literal model filename')
    if not re.fullmatch(r'[0-9a-f]{64}', result['MODEL_SHA256']):
        raise ValueError('Use a pinned model hash')
    if result['DEVICE'] not in ('Vulkan0', 'none'):
        raise ValueError('Use the local Vulkan GPU or isolated CPU profile')
    if not (1 <= int(result['SLOTS']) <= 8 and 256 <= int(result['TOKENS_PER_SLOT']) <= 2048
            and 256 <= int(result['COMPUTE_BATCH']) <= 4096 and 1 <= int(result['THREADS']) <= 4):
        raise ValueError('Invalid experimental capacity')
    if int(result['COMPUTE_BATCH']) < int(result['TOKENS_PER_SLOT']):
        raise ValueError('The complete query/passage prompt must fit the microbatch')
    return result


def command(config):
    result = ['docker', 'run', '--rm', '--name', NAME, '--label', 'eqarchives.managed=reranker-pilot',
              '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
              '--read-only', '--cpus', '2', '--memory', '4g', '--pids-limit', '128',
              '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m', '--env', 'XDG_CACHE_HOME=/tmp/cache',
              '--publish', f"{config['BIND_ADDRESS']}:{config['BIND_PORT']}:8080",
              '--mount', f'type=bind,source={MODELS},target=/models,readonly',
              '--mount', f'type=bind,source={KEY},target=/run/secrets/api_key,readonly']
    if config['DEVICE'] == 'Vulkan0':
        result += ['--device', '/dev/dri/renderD128', '--group-add', '109']
    result += ['--entrypoint', '/app/llama-server', config['LLAMA_IMAGE'],
               '--model', '/models/' + config['MODEL_FILE'], '--alias', config['MODEL_ALIAS'],
               '--host', '0.0.0.0', '--port', '8080', '--reranking', '--pooling', 'rank',
               '--ctx-size', str(int(config['SLOTS']) * int(config['TOKENS_PER_SLOT'])),
               '--parallel', config['SLOTS'], '--batch-size', config['COMPUTE_BATCH'],
               '--ubatch-size', config['COMPUTE_BATCH'], '--threads', config['THREADS'],
               '--threads-batch', config['THREADS'], '--device', config['DEVICE'],
               '--gpu-layers', '99' if config['DEVICE'] == 'Vulkan0' else '0',
               '--flash-attn', 'on', '--fit', 'off', '--no-cache-prompt', '--cache-ram', '0',
               '--api-key-file', '/run/secrets/api_key', '--no-webui', '--no-slots',
               '--metrics', '--timeout', '30', '--log-verbosity', '1']
    if config['DEVICE'] == 'none':
        result += ['--no-op-offload']
    return result


def scores(response, count, alias):
    """Reject incomplete, misidentified or nonfinite scores, never synthesize zero."""
    if not isinstance(response, dict) or response.get('model') != alias:
        raise ValueError('Reranker model identity differs')
    rows = response.get('results')
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError('Incomplete reranker response')
    result = [None] * count
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid reranker result')
        index, value = row.get('index'), row.get('relevance_score')
        if (type(index) is not int or not 0 <= index < count or result[index] is not None
                or type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError('Invalid reranker score or index')
        result[index] = value
    return result


def healthy(config, opener=urllib.request.urlopen):
    try:
        body = {'model': config['MODEL_ALIAS'], 'query': 'Which planet is known as the Red Planet?',
                'documents': ['Mars is known as the Red Planet.', 'Sourdough bread uses flour and water.']}
        request = urllib.request.Request(
            f"http://{config['BIND_ADDRESS']}:{config['BIND_PORT']}/v1/rerank", data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY.read_text().strip()})
        with opener(request, timeout=15) as response:
            values = scores(json.load(response), 2, config['MODEL_ALIAS'])
        return values[0] > 0.5 and values[0] - values[1] > 0.1
    except Exception:
        return False  # Never print upstream bodies, request text or credentials.


def verify_model(config):
    digest = hashlib.sha256()
    with (MODELS / config['MODEL_FILE']).open('rb') as stream:
        while part := stream.read(1024 * 1024):
            digest.update(part)
    if digest.hexdigest() != config['MODEL_SHA256']:
        raise ValueError('Reranker model checksum differs')


def remove_owned_container():
    result = subprocess.run(['docker', 'inspect', '--format', '{{json .Config.Labels}}', NAME],
                            capture_output=True, text=True)
    if result.returncode == 0:
        labels = json.loads(result.stdout)
        if not isinstance(labels, dict) or labels.get('eqarchives.managed') != 'reranker-pilot':
            raise ValueError('Refusing to replace an unrelated container')
        subprocess.run(['docker', 'rm', '-f', NAME], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'health', 'validate'))
    args = parser.parse_args()
    config = settings()
    if args.action == 'health':
        success = healthy(config)
        print('Reranker inference healthy' if success else 'Reranker inference unavailable')
        return 0 if success else 1
    verify_model(config)
    if not KEY.read_text().strip():
        raise ValueError('Reranker credential is empty')
    if args.action == 'validate':
        print('Verified reranker model and private configuration')
        return 0
    remove_owned_container()
    os.execvp('docker', command(config))


if __name__ == '__main__':
    raise SystemExit(main())
