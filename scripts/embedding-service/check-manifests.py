"""Regression check for the fully transformed Kubernetes embedding routes."""
import sys
from pathlib import Path
import yaml

resources = {(item['kind'], item['metadata']['name']): item for item in yaml.safe_load_all(sys.stdin) if item}
service = resources['Service', 'nomic-embeddings-rtx3080']
endpoints = resources['Endpoints', 'nomic-embeddings-rtx3080']
config = dict(line.split('=', 1) for line in Path(__file__).with_name('cuda.env').read_text().splitlines()
              if line and not line.startswith('#'))
assert not service['spec'].get('selector'), 'CUDA service must not select cluster pods'
assert service['spec']['ports'][0]['targetPort'] == int(config['BIND_PORT'])
assert endpoints['subsets'] == [{'addresses': [{'ip': config['BIND_ADDRESS']}],
                                'ports': [{'name': 'http', 'port': int(config['BIND_PORT']), 'protocol': 'TCP'}]}]
for item in (service, endpoints):
    assert item['metadata']['labels']['app.kubernetes.io/part-of'] == 'eqarchives-embeddings'
local = resources['Service', 'nomic-embeddings']
assert local['spec']['selector']['app'] == 'nomic-embeddings'
print('Verified selectorless CUDA routing and unchanged local Vulkan service identity.')
