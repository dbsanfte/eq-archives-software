from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('handoff', Path(__file__).with_name('render-reindex.py'))
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)

class HandoffTests(unittest.TestCase):
    def previous(self):
        return {'metadata': {'name':'old', 'namespace':'eqarchives-es', 'uid':'original-uid'}, 'spec': {
            'backoffLimit':6, 'podFailurePolicy':{'rules':[]},
            'template':{'metadata':{'labels':{'controller-uid':'generated'}}, 'spec':{
                'containers':[{'name':'indexer', 'image':'old', 'command':['python','-u','/recovery/runner.py'],
                               'env':[{'name':'SKIP_LLM_ENRICHMENT','value':'true'}],
                               'volumeMounts':[{'name':'archive','readOnly':True}]}],
                'volumes':[{'name':'archive','hostPath':{'path':'/mnt/samsung/eq-archives-html-reindex'}}]}}}}

    def test_handoff_stays_pending_with_same_sources_secrets_and_recovery(self):
        previous = self.previous(); original = deepcopy(previous)
        job = handoff.render(previous, 'reindex-dual-gpu', 'eqarchives-indexer:rtx3080-'+'a'*40)
        self.assertEqual(previous, original)
        self.assertEqual(job['spec']['parallelism'], 0)
        self.assertNotIn('suspend', job['spec'])
        self.assertNotIn('metadata', job['spec']['template'])
        self.assertEqual(job['spec']['template']['spec']['volumes'], previous['spec']['template']['spec']['volumes'])
        container = job['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['command'], ['python','-u','/recovery/runner.py'])
        env = {row['name']:row['value'] for row in container['env']}
        self.assertEqual(env['REINDEX_WORKERS'], '4')
        self.assertEqual(env['REINDEX_SECONDARY_WORKERS'], '1')
        self.assertEqual(env['SKIP_LLM_ENRICHMENT'], 'true')

    def test_rejects_preparation_writable_sources_or_unrecognized_entrypoint(self):
        for change in ('init', 'write', 'command', 'enrichment'):
            previous = self.previous(); pod = previous['spec']['template']['spec']; c = pod['containers'][0]
            if change == 'init': pod['initContainers'] = []
            if change == 'write': c['volumeMounts'][0]['readOnly'] = False
            if change == 'command': c['command'] = ['different']
            if change == 'enrichment': c['env'][0]['value'] = 'false'
            with self.assertRaises(ValueError):
                handoff.render(previous, 'reindex-dual-gpu', 'eqarchives-indexer:rtx3080-'+'a'*40)

if __name__ == '__main__': unittest.main()
