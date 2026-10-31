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
# Online search and MCP must retain the low-latency Vulkan route.
for name, variable in (('search-eqarchives', 'OPENAI_URL'), ('eqarchives-mcp', 'EMBEDDING_URL')):
    containers = resources['Deployment', name]['spec']['template']['spec']['containers']
    env = {row['name']: row.get('value') for container in containers for row in container.get('env', [])}
    assert env[variable] == 'http://nomic-embeddings.eqarchives-es.svc.cluster.local:8080', 'Online queries must stay on Vulkan'
print('Verified CUDA indexing route and dedicated Vulkan routes for online queries.')
