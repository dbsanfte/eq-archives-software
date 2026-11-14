"""Prepare an operator-authorized CUDA-only handoff; never modify the live Job."""
import argparse
import copy
import json
import re
import sys
from pathlib import Path


def render(previous, name, image, *, gemma=False, images=False, text_hosts=None):
    if images and text_hosts:raise ValueError('Use separate text and image workers')
    if text_hosts and any(not re.fullmatch(r'[a-zA-Z0-9_.:\-]{1,253}',h) for h in text_hosts):
        raise ValueError('Use literal archive host names')
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
    env = {item['name']: item for item in container['env']
           if not item['name'].startswith('REINDEX_SECONDARY_')}
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
              'OPENAI_ENDPOINT_EMBEDDINGS': 'http://nomic-embeddings-rtx3080.eqarchives-es.svc.cluster.local:8080/v1',
              'NOMIC_EMBEDDING_PROFILE': 'cuda', 'NOMIC_EMBEDDING_BATCH_SIZE': '8',
              'NOMIC_EMBEDDING_ENCODING': 'base64', 'REINDEX_WORKERS': '4',
              'REINDEX_PREPARATION_WORKERS': '2', 'OPENBLAS_NUM_THREADS': '1'}
    if gemma or images or text_hosts:
        values.update(OPENAI_EMBEDDING_MODEL_NAME='embeddinggemma-2-768-v1',
                      REINDEX_ENTRYPOINT='reindex_embeddings.pyc', GEMMA_EMBEDDING_PROFILE='cuda',
                      GEMMA_EMBEDDING_BATCH_SIZE='8', REINDEX_PRIORITIZE_UNCONVERTED='true')
    if (gemma or text_hosts) and not images:
        pins=dict(line.split('=',1) for line in (Path(__file__).parents[1]/'cuda-batch-server/deployment.env').read_text().splitlines()
                  if line and not line.startswith('#'))
        if not re.fullmatch(r'eqarchives-cuda-batch@sha256:[0-9a-f]{64}',pins.get('CUDA_IMAGE','')):
            raise ValueError('Invalid CUDA batch cache identity')
        endpoint='http://cuda-batch-inference.eqarchives-es.svc.cluster.local:8080/v1'
        values.update(OPENAI_ENDPOINT=endpoint,OPENAI_ENDPOINT_EMBEDDINGS=endpoint,
                      EMBEDDING_CACHE_BACKEND_IMAGE=pins['CUDA_IMAGE'])
    if (gemma or text_hosts) and not images:
        values.update(EMBEDDING_CACHE_PATH='/indexing-state/embeddings.sqlite',
                      DISCUSSION_CATALOG_PATH='/indexing-state/discussions.sqlite',
                      REINDEX_DIAGNOSTICS_PATH='/indexing-state/reindex-failures.sqlite',REINDEX_JOB_NAME=name)
        state={'name':'indexing-state','hostPath':{'path':'/mnt/samsung/eqarchives-curation/indexing-state','type':'Directory'}}
        mount={'name':'indexing-state','mountPath':'/indexing-state'}
        existing=next((v for v in pod['volumes'] if v['name']=='indexing-state'),None)
        existing_mount=next((v for v in container['volumeMounts'] if v['name']=='indexing-state'),None)
        if existing is not None and existing!=state or existing_mount is not None and existing_mount!=mount:
            raise ValueError('Preserve the shared private indexing state location')
        if existing is None:pod['volumes'].append(state)
        if existing_mount is None:container['volumeMounts'].append(mount)
        security=pod.setdefault('securityContext',{})
        security['supplementalGroups']=list(dict.fromkeys(security.get('supplementalGroups',[])+[10001]))
    if gemma and not images and not text_hosts:
        values['REINDEX_PUBLICATION_REPO']='/publication'
        publication={'name':'publication','hostPath':{'path':'/mnt/samsung/eqarchives-curation/publisher.git','type':'Directory'}}
        publication_mount={'name':'publication','mountPath':'/publication','readOnly':True}
        for items,item in ((pod['volumes'],publication),(container['volumeMounts'],publication_mount)):
            previous_item=next((v for v in items if v['name']==item['name']),None)
            if previous_item is not None and previous_item!=item:raise ValueError('Preserve the read-only publication source location')
            if previous_item is None:items.append(item)
        pod.setdefault('securityContext',{}).update(runAsUser=10001,runAsGroup=10001)
        # Container identity overrides pod defaults on Kubernetes. Retain its
        # hardening while matching the owner of the private publication store.
        container.setdefault('securityContext',{}).update(runAsUser=10001,runAsGroup=10001)
    if images:
        vision='http://gemma-image-embeddings.eqarchives-es.svc.cluster.local:8080/v1'
        values.update(REINDEX_ENTRYPOINT='index_images.pyc', OPENAI_ENDPOINT=vision,
                      OPENAI_ENDPOINT_EMBEDDINGS=vision, GEMMA_IMAGE_ENDPOINT=vision,
                      REINDEX_WORKERS='1', REINDEX_PREPARATION_WORKERS='0')
        pod['volumes'].append({'name':'image-state','hostPath':{'path':'/mnt/samsung/eqarchives-images','type':'Directory'}})
        container['volumeMounts'].append({'name':'image-state','mountPath':'/image-state'})
    if text_hosts:
        values.update(REINDEX_ENTRYPOINT='index_websites.pyc',REINDEX_WORKERS='1',REINDEX_PREPARATION_WORKERS='0',
                      TEXT_IMPORT_HOSTS=','.join(dict.fromkeys(text_hosts)),TEXT_IMPORT_STATE='/text-state/inventory.sqlite')
        state={'name':'text-state','hostPath':{'path':'/mnt/samsung/eqarchives-text-import','type':'Directory'}}
        mount={'name':'text-state','mountPath':'/text-state'}
        existing=next((v for v in pod['volumes'] if v['name']=='text-state'),None)
        existing_mount=next((v for v in container['volumeMounts'] if v['name']=='text-state'),None)
        if existing is not None and existing!=state or existing_mount is not None and existing_mount!=mount:
            raise ValueError('Preserve the existing text import cursor mount')
        if existing is None:pod['volumes'].append(state)
        if existing_mount is None:container['volumeMounts'].append(mount)
    env.update({key: {'name': key, 'value': value} for key, value in values.items()})
    container['env'] = list(env.values())
    container['image'] = image
    # Bound CPU and memory while allowing concurrent extraction/tokenization.
    container['resources'] = {'requests': {'cpu': '500m', 'memory': '512Mi'},
                              'limits': {'cpu': '4', 'memory': '4Gi'}}
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {
        'name': name, 'namespace': previous['metadata']['namespace'],
        'annotations': {'eqarchives.org/predecessor-uid': previous['metadata']['uid'],
                        'eqarchives.org/work-kind': 'missing-website-text' if text_hosts else 'images' if images else 'text-passages' if gemma else 'legacy-text'}},
        'spec': {'backoffLimit': previous['spec']['backoffLimit'], 'parallelism': 0, 'completions': 1,
                 'podFailurePolicy': copy.deepcopy(previous['spec']['podFailurePolicy']),
                 'template': {'spec': pod}}}
    if gemma and not images and not text_hosts:
        rules=job['spec']['podFailurePolicy']['rules']
        if not any(r.get('action')=='FailJob' and 43 in r.get('onExitCodes',{}).get('values',[]) for r in rules):
            rules.insert(0,{'action':'FailJob','onExitCodes':{'containerName':'indexer','operator':'In','values':[43]}})
    return job


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--gemma', action='store_true', help='Start a new, independent model-version checkpoint')
    parser.add_argument('--images', action='store_true', help='Create-only image backfill with a persistent local inventory')
    parser.add_argument('--text-host',action='append',help='Create-only missing text import for an explicit archive host (repeatable)')
    args = parser.parse_args()
    print(json.dumps(render(json.load(sys.stdin), args.name, args.image, gemma=args.gemma, images=args.images,text_hosts=args.text_host)))
