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
        self.assertFalse(any(name.startswith('REINDEX_SECONDARY_') for name in env))
        self.assertEqual(env['REINDEX_PREPARATION_WORKERS'], '2')
        self.assertEqual(env['SKIP_LLM_ENRICHMENT'], 'true')

    def test_gemma_sources_include_private_publication_objects_readonly_and_fail_once_on_content(self):
        previous=self.previous();before=deepcopy(previous)
        job=handoff.render(previous,'reindex-source-recovery','eqarchives-indexer:rtx3080-'+'a'*40,gemma=True)
        assert previous==before
        pod=job['spec']['template']['spec'];container=pod['containers'][0]
        assert pod['securityContext']['runAsUser']==pod['securityContext']['runAsGroup']==10001
        assert {'name':'publication','mountPath':'/publication','readOnly':True} in container['volumeMounts']
        assert next(v for v in pod['volumes'] if v['name']=='publication')['hostPath']['path']=='/mnt/samsung/eqarchives-curation/publisher.git'
        assert {e['name']:e.get('value') for e in container['env']}['REINDEX_PUBLICATION_REPO']=='/publication'
        assert job['spec']['podFailurePolicy']['rules'][0]['onExitCodes']['values']==[43]
        job['metadata']['uid']='created-job-uid'
        repeated=handoff.render(job,'reindex-source-recovery-next','eqarchives-indexer:rtx3080-'+'b'*40,gemma=True)
        assert sum(v['name']=='publication' for v in repeated['spec']['template']['spec']['volumes'])==1

    def test_gemma_publication_identity_overrides_inherited_container_uid(self):
        previous=self.previous()
        original_security={'runAsUser':1000,'runAsGroup':1000,'runAsNonRoot':True,
                           'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}}
        previous['spec']['template']['spec']['containers'][0]['securityContext']=deepcopy(original_security)
        before=deepcopy(previous)
        job=handoff.render(previous,'reindex-private-source','eqarchives-indexer:rtx3080-'+'a'*40,gemma=True)
        pod=job['spec']['template']['spec'];security=pod['containers'][0]['securityContext']
        self.assertEqual(security['runAsUser'],10001)
        self.assertEqual(security['runAsGroup'],10001)
        self.assertEqual({k:v for k,v in security.items() if k not in ('runAsUser','runAsGroup')},
                         {k:v for k,v in original_security.items() if k not in ('runAsUser','runAsGroup')})
        self.assertEqual(previous,before)
        legacy=handoff.render(previous,'reindex-legacy-source','eqarchives-indexer:rtx3080-'+'b'*40)
        self.assertEqual(legacy['spec']['template']['spec']['containers'][0]['securityContext'],original_security)

    def test_gemma_handoff_has_its_own_checkpoint_and_keeps_cuda_only(self):
        previous = self.previous()
        job = handoff.render(previous, 'reindex-gemma', 'eqarchives-indexer:rtx3080-'+'a'*40, gemma=True)
        env = {e['name']: e['value'] for e in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['REINDEX_ENTRYPOINT'], 'reindex_embeddings.pyc')
        self.assertEqual(env['OPENAI_EMBEDDING_MODEL_NAME'], 'embeddinggemma-2-768-v1')
        self.assertEqual(env['GEMMA_EMBEDDING_BATCH_SIZE'], '8')
        self.assertEqual(env['REINDEX_WORKERS'], '4')
        self.assertEqual(env['REINDEX_PREPARATION_WORKERS'], '2')
        self.assertIn('cuda-batch-inference.', env['OPENAI_ENDPOINT'])
        self.assertEqual(env['OPENAI_ENDPOINT'], env['OPENAI_ENDPOINT_EMBEDDINGS'])
        self.assertRegex(env['EMBEDDING_CACHE_BACKEND_IMAGE'], r'^eqarchives-cuda-batch@sha256:[0-9a-f]{64}$')
        self.assertEqual(env['SKIP_LLM_ENRICHMENT'], 'true')
        self.assertEqual(job['spec']['parallelism'], 0)
        self.assertEqual(env['EMBEDDING_CACHE_PATH'],'/indexing-state/embeddings.sqlite')
        self.assertEqual(env['REINDEX_DIAGNOSTICS_PATH'],'/indexing-state/reindex-failures.sqlite')
        pod=job['spec']['template']['spec']
        self.assertIn(10001,pod['securityContext']['supplementalGroups'])
        self.assertEqual(next(v for v in pod['volumes'] if v['name']=='indexing-state')['hostPath']['path'],'/mnt/samsung/eqarchives-curation/indexing-state')
        job['metadata']['uid']='new-uid'
        resumed=handoff.render(job,'reindex-gemma-next','eqarchives-indexer:rtx3080-'+'b'*40,gemma=True)
        self.assertEqual(resumed['spec']['template']['spec']['volumes'],pod['volumes'])

    def test_cuda_handoff_removes_inherited_secondary_and_embedding_overrides(self):
        previous = self.previous()
        previous['spec']['template']['spec']['containers'][0]['env'] += [
            {'name': 'REINDEX_SECONDARY_ENDPOINT', 'value': 'http://vulkan/v1'},
            {'name': 'REINDEX_SECONDARY_WORKERS', 'value': '1'},
            {'name': 'OPENAI_ENDPOINT_EMBEDDINGS', 'value': 'http://vulkan/v1'}]
        original = deepcopy(previous)
        job = handoff.render(previous, 'reindex-cuda-only', 'eqarchives-indexer:rtx3080-'+'a'*40)
        env = {e['name']: e.get('value') for e in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(previous, original)
        self.assertFalse(any(name.startswith('REINDEX_SECONDARY_') for name in env))
        self.assertEqual(env['OPENAI_ENDPOINT_EMBEDDINGS'], env['OPENAI_ENDPOINT'])
        self.assertIn('nomic-embeddings-rtx3080.', env['OPENAI_ENDPOINT'])

    def test_image_backfill_has_its_own_persistent_state_without_mutating_text_sources(self):
        previous=self.previous();original=deepcopy(previous)
        job=handoff.render(previous,'reindex-images','eqarchives-indexer:rtx3080-'+'a'*40,images=True)
        pod=job['spec']['template']['spec'];container=pod['containers'][0]
        env={row['name']:row['value'] for row in container['env']}
        self.assertEqual(previous,original)
        self.assertEqual(job['spec']['parallelism'],0)
        self.assertEqual(env['REINDEX_ENTRYPOINT'],'index_images.pyc')
        self.assertEqual(env['SKIP_LLM_ENRICHMENT'],'true')
        self.assertIn('gemma-image-embeddings.',env['OPENAI_ENDPOINT'])
        self.assertEqual(env['REINDEX_WORKERS'],'1')
        self.assertEqual(container['volumeMounts'][0],original['spec']['template']['spec']['containers'][0]['volumeMounts'][0])
        self.assertEqual(pod['volumes'][-1]['hostPath'],{'path':'/mnt/samsung/eqarchives-images','type':'Directory'})

    def test_rejects_preparation_writable_sources_or_unrecognized_entrypoint(self):
        for change in ('init', 'write', 'command', 'enrichment'):
            previous = self.previous(); pod = previous['spec']['template']['spec']; c = pod['containers'][0]
            if change == 'init': pod['initContainers'] = []
            if change == 'write': c['volumeMounts'][0]['readOnly'] = False
            if change == 'command': c['command'] = ['different']
            if change == 'enrichment': c['env'][0]['value'] = 'false'
            with self.assertRaises(ValueError):
                handoff.render(previous, 'reindex-dual-gpu', 'eqarchives-indexer:rtx3080-'+'a'*40)



class TextImportHandoffTests(unittest.TestCase):
    previous=HandoffTests.previous
    def test_explicit_text_scope_uses_create_only_entrypoint_and_independent_persistent_state(self):
        old=self.previous();original=deepcopy(old)
        job=handoff.render(old,'reindex-missing-text','eqarchives-indexer:rtx3080-'+'a'*40,text_hosts=['necro.eqclasses.com','www.eqatlas.com'])
        c=job['spec']['template']['spec']['containers'][0];env={e['name']:e['value'] for e in c['env']}
        self.assertEqual(old,original);self.assertEqual(job['spec']['parallelism'],0)
        self.assertEqual(env['REINDEX_ENTRYPOINT'],'index_websites.pyc');self.assertEqual(env['REINDEX_WORKERS'],'1')
        self.assertEqual(env['TEXT_IMPORT_HOSTS'],'necro.eqclasses.com,www.eqatlas.com');self.assertEqual(env['SKIP_LLM_ENRICHMENT'],'true')
        self.assertIn('cuda-batch-inference',env['OPENAI_ENDPOINT']);self.assertNotIn('initContainers',job['spec']['template']['spec'])
        self.assertEqual(c['volumeMounts'][-1],{'name':'text-state','mountPath':'/text-state'})
        self.assertEqual(job['metadata']['annotations']['eqarchives.org/work-kind'],'missing-website-text')

    def test_invalid_scope_or_mixed_vision_writer_is_refused(self):
        for kw in ({'text_hosts':['../site']},{'text_hosts':['site.example'],'images':True}):
            with self.assertRaises(ValueError):handoff.render(self.previous(),'reindex-missing-text','eqarchives-indexer:rtx3080-'+'a'*40,**kw)

    def test_repeated_handoffs_preserve_single_cache_and_import_cursor_mounts(self):
        old=self.previous()
        first=handoff.render(old,'reindex-missing-text','eqarchives-indexer:rtx3080-'+'a'*40,text_hosts=['necro.eqclasses.com'])
        first['metadata']['uid']='first-uid'
        second=handoff.render(first,'reindex-missing-text-next','eqarchives-indexer:rtx3080-'+'b'*40,text_hosts=['necro.eqclasses.com'])
        self.assertEqual(first['spec']['template']['spec']['volumes'],second['spec']['template']['spec']['volumes'])
        self.assertEqual(first['spec']['template']['spec']['containers'][0]['volumeMounts'],second['spec']['template']['spec']['containers'][0]['volumeMounts'])
        first['spec']['template']['spec']['volumes'][-1]['hostPath']['path']='/wrong-cursor'
        with self.assertRaises(ValueError):handoff.render(first,'reindex-refused','eqarchives-indexer:rtx3080-'+'c'*40,text_hosts=['necro.eqclasses.com'])

if __name__ == '__main__':unittest.main()
