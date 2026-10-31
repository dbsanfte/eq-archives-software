"""Prepare an operator-authorized dual-GPU handoff; never modify the live Job."""
import argparse
import copy
import json
import re
import sys


def render(previous, name, image):
    if not re.fullmatch(r'reindex-[a-z0-9-]{1,54}', name) or name == previous['metadata']['name']:
        raise ValueError('Use a new descriptive reindex Job name')
    if not re.fullmatch(r'eqarchives-indexer:rtx3080-[0-9a-f]{40}', image):
        raise ValueError('Use the locally tested image tagged with the complete merged Git SHA')
    pod = copy.deepcopy(previous['spec']['template']['spec'])
    if len(pod['containers']) != 1:
        raise ValueError('Expected one text reindex container')
    container = pod['containers'][0]
    if container['command'] != ['python', '-u', '/recovery/runner.py'] or container.get('args'):
        raise ValueError('Expected the unrestricted checkpointed recovery runner')
    env = {item['name']: item for item in container['env']}
    if env['SKIP_LLM_ENRICHMENT'].get('value') != 'true':
        raise ValueError('Do not enable paid enrichment')
    archive = next(v for v in pod['volumes'] if v['name'] == 'archive')
    if archive.get('hostPath', {}).get('path') != '/mnt/samsung/eq-archives-html-reindex':
        raise ValueError('Retain the prepared source checkout')
    if not any(v['name'] == 'archive' and v.get('readOnly') for v in container['volumeMounts']):
        raise ValueError('Sources must be read-only')
    if 'initContainers' in pod:
        raise ValueError('Do not repeat source preparation')
    values = {'OPENAI_ENDPOINT': 'http://nomic-embeddings-rtx3080.eqarchives-es.svc.cluster.local:8080/v1',
              'NOMIC_EMBEDDING_PROFILE': 'cuda', 'NOMIC_EMBEDDING_BATCH_SIZE': '8',
              'NOMIC_EMBEDDING_ENCODING': 'base64', 'REINDEX_WORKERS': '4',
              'REINDEX_SECONDARY_ENDPOINT': 'http://nomic-embeddings.eqarchives-es.svc.cluster.local:8080/v1',
              'REINDEX_SECONDARY_WORKERS': '1', 'REINDEX_PREPARATION_WORKERS': '2', 'OPENBLAS_NUM_THREADS': '1'}
    env.update({key: {'name': key, 'value': value} for key, value in values.items()})
    container['env'] = list(env.values())
    container['image'] = image
    # Bound CPU and memory while allowing concurrent extraction/tokenization.
    container['resources'] = {'requests': {'cpu': '500m', 'memory': '512Mi'},
                              'limits': {'cpu': '4', 'memory': '4Gi'}}
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {
        'name': name, 'namespace': previous['metadata']['namespace'],
        'annotations': {'eqarchives.org/predecessor-uid': previous['metadata']['uid']}},
        'spec': {'backoffLimit': previous['spec']['backoffLimit'], 'parallelism': 0, 'completions': 1,
                 'podFailurePolicy': copy.deepcopy(previous['spec']['podFailurePolicy']),
                 'template': {'spec': pod}}}
    return job


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    print(json.dumps(render(json.load(sys.stdin), args.name, args.image)))
