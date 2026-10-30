"""Render a new recovery Job from a failed Job, without modifying the predecessor."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys


def render(previous, name, runner, *, probe_id=None):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,61}[a-z0-9]', name):
        raise ValueError('Use a new Kubernetes Job name of 2–63 characters')
    if name == previous['metadata']['name']:
        raise ValueError('A continuation must have a new name')
    if not any(c['type'] == 'Failed' and c['status'] == 'True'
               for c in previous.get('status', {}).get('conditions', [])):
        raise ValueError('The predecessor must be a terminal failed Job')
    pod = copy.deepcopy(previous['spec']['template']['spec'])
    if len(pod['containers']) != 1 or pod['containers'][0]['name'] != 'indexer':
        raise ValueError('Expected a single text reindex container')
    container = pod['containers'][0]
    if container.get('command') != ['python', '-u', 'reindex_text.pyc']:
        raise ValueError('Expected the checkpointed text reindex entry point')
    if container.get('args'):
        raise ValueError('Continuation must start from the unrestricted broad Job')
    env = {item['name']: item.get('value') for item in container.get('env', [])}
    if env.get('SKIP_LLM_ENRICHMENT') != 'true' or env.get('NOMIC_EMBEDDING_BATCH_SIZE') != '2':
        raise ValueError('Retain the no-enrichment, two-chunk text repair policy')
    archive = next(v for v in pod['volumes'] if v['name'] == 'archive')
    if archive.get('hostPath', {}).get('path') != '/mnt/samsung/eq-archives-html-reindex':
        raise ValueError('Retain the dedicated prepared source checkout')
    if not any(v['name'] == 'archive' and v.get('readOnly') for v in container['volumeMounts']):
        raise ValueError('Archive sources must be read-only')
    if any(v['name'] == 'recovery' for v in pod['volumes']):
        raise ValueError('Unexpected preexisting recovery volume')
    revision = hashlib.sha256(runner.encode()).hexdigest()
    config_name = 'reindex-recovery-' + revision[:16]
    namespace = previous['metadata']['namespace']
    config = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'immutable': True,
              'metadata': {'name': config_name, 'namespace': namespace},
              'data': {'runner.py': runner}}
    # Deliberately do not copy generated selectors, controller labels, status,
    # source-preparation init containers, deadlines or garbage-collection TTLs.
    pod.pop('initContainers', None)
    pod['restartPolicy'] = 'Never'
    pod['automountServiceAccountToken'] = False
    pod['volumes'].append({'name': 'recovery', 'configMap': {'name': config_name}})
    container['volumeMounts'].append({'name': 'recovery', 'mountPath': '/recovery', 'readOnly': True})
    container['command'] = ['python', '-u', '/recovery/runner.py']
    if probe_id:
        if not probe_id.startswith(('websites/', 'newsgroups/', 'mailing-lists/')) or '..' in probe_id.split('/'):
            raise ValueError('Probe requires an exact supported archive ID')
        container['args'] = ['--id', probe_id]
    job = {'apiVersion': 'batch/v1', 'kind': 'Job',
           'metadata': {'name': name, 'namespace': namespace,
                        'annotations': {'eqarchives.org/predecessor-uid': previous['metadata']['uid'],
                                        'eqarchives.org/recovery-sha256': revision}},
           'spec': {'backoffLimit': 6, 'parallelism': 1, 'completions': 1,
                    'podFailurePolicy': {'rules': [
                        {'action': 'FailJob', 'onExitCodes': {'containerName': 'indexer',
                            'operator': 'In', 'values': [42]}},
                        {'action': 'Ignore', 'onPodConditions': [{'type': 'DisruptionTarget', 'status': 'True'}]},
                        {'action': 'Ignore', 'onExitCodes': {'containerName': 'indexer',
                            'operator': 'In', 'values': [143, 255]}}]},
                    'template': {'spec': pod}}}
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [config, job]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--probe-id')
    args = parser.parse_args()
    print(json.dumps(render(json.load(sys.stdin), args.name,
                           Path(__file__).with_name('runner.py').read_text(), probe_id=args.probe_id)))
