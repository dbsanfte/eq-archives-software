"""Private serving, scoring integrity and source-bound ordering regressions."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import service
import benchmark as pilot


def response(rows, alias=pilot.ALIAS):
    return {'model': alias, 'results': rows}


class CharacterTokenizer:
    def encode(self, value, **_):
        return SimpleNamespace(ids=list(map(ord, value)))

    def decode(self, value):
        return ''.join(map(chr, value))


class ServingTests(unittest.TestCase):
    def config(self):
        return service.settings(Path(__file__).parent)

    def test_private_gpu_command_preserves_existing_embedding_services(self):
        config = self.config();before = deepcopy(config);args = service.command(config)
        self.assertEqual(config, before)
        for value in ('127.0.0.1:8083:8080', '--read-only', '--cap-drop', '--no-webui', '--no-slots',
                      '--reranking', '/dev/dri/renderD128', '--api-key-file'):
            self.assertIn(value, args)
        self.assertEqual(args[args.index('--name')+1], 'eqarchives-reranker-pilot')
        self.assertNotIn('--privileged', args)
        self.assertFalse(any('nomic-embeddings' in value for value in args))
        self.assertEqual(args[args.index('--pooling')+1], 'rank')
        self.assertEqual(args[args.index('--ctx-size')+1], '2048')

    def test_cpu_profile_has_no_gpu_device_and_no_op_offload(self):
        config = {**self.config(), 'DEVICE': 'none'};args = service.command(config)
        self.assertNotIn('/dev/dri/renderD128', args)
        self.assertIn('--no-op-offload', args)
        self.assertEqual(args[args.index('--gpu-layers')+1], '0')

    def test_cuda_profile_is_independent_and_uses_pinned_cdi_image(self):
        config={**self.config(),'DEVICE':'CUDA0'};args=service.command(config)
        self.assertIn('nvidia.com/gpu=all',args)
        self.assertNotIn('/dev/dri/renderD128',args)
        self.assertIn(config['CUDA_IMAGE'],args)
        self.assertNotIn(config['LLAMA_IMAGE'],args)
        self.assertEqual(args[args.index('--gpu-layers')+1],'99')

    def test_invalid_bind_pins_and_capacity_are_rejected(self):
        original = (Path(__file__).parent/'runtime.env').read_text()
        mutations = [('127.0.0.1', '0.0.0.0'), ('SLOTS=2', 'SLOTS=99'), ('TOKENS_PER_SLOT=1024', 'TOKENS_PER_SLOT=2048'),
                     ('THREADS=2', 'THREADS=99'), ('DEVICE=Vulkan0', 'DEVICE=arbitrary'),
                     ('MODEL_FILE=qwen3-reranker-0.6b-q8_0.gguf', 'MODEL_FILE=../model.gguf'),
                     ('@sha256:', ':latest-'), ('BIND_PORT=8083', 'BIND_PORT=80')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for old, new in mutations:
                with self.subTest(new=new):
                    (root/'runtime.env').write_text(original.replace(old, new))
                    with self.assertRaises(ValueError):service.settings(root)
            (root/'runtime.env').write_text(original+'\nSLOTS=2\n')
            with self.assertRaises(ValueError):service.settings(root)

    def test_model_corruption_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root/'model.gguf').write_bytes(b'tested model')
            config = {'MODEL_FILE': 'model.gguf', 'MODEL_SHA256': hashlib.sha256(b'tested model').hexdigest()}
            with patch.object(service, 'MODELS', root):
                service.verify_model(config)
                (root/'model.gguf').write_bytes(b'changed model')
                with self.assertRaisesRegex(ValueError, 'checksum'):service.verify_model(config)

    def test_only_the_owned_pilot_container_can_be_replaced(self):
        for labels in ({}, {'eqarchives.managed':'cuda-embeddings'}):
            with patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(labels))) as run:
                with self.assertRaisesRegex(ValueError,'unrelated'):service.remove_owned_container()
                self.assertEqual(run.call_count,1)
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='')) as run:
            service.remove_owned_container();self.assertEqual(run.call_count,1)
        with patch('subprocess.run',side_effect=[SimpleNamespace(returncode=0,stdout='{"eqarchives.managed":"reranker-pilot"}'),SimpleNamespace(returncode=0)]) as run:
            service.remove_owned_container()
            self.assertEqual(run.call_args.args[0],['docker','rm','-f','eqarchives-reranker-pilot'])

    def test_sorted_scores_are_restored_to_original_document_indices(self):
        value = response([{'index': 1, 'relevance_score': .9}, {'index': 0, 'relevance_score': .1}])
        self.assertEqual(service.scores(value, 2, pilot.ALIAS), [.1, .9])

    def test_missing_duplicate_nonfinite_and_wrong_model_scores_fail(self):
        cases = [response([]), response([{'index':0,'relevance_score':.4}]*2),
                 response([{'index':0,'relevance_score':float('nan')},{'index':1,'relevance_score':.5}]),
                 response([{'index':True,'relevance_score':.5},{'index':0,'relevance_score':.5}]),
                 response([{'index':0,'relevance_score':True},{'index':1,'relevance_score':.5}]),
                 response([{'index':0,'relevance_score':1.1},{'index':1,'relevance_score':.5}]),
                 response([None,None]), response([{'index':0,'relevance_score':.4},{'index':1,'relevance_score':.6}], 'other')]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):service.scores(value, 2, pilot.ALIAS)

    def test_health_checks_actual_relevance_instead_of_http_status(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory)/'key';key.write_text('private-test-key')
            with patch.object(service, 'KEY', key):
                for values, expected in (([.9,.1],True),([.2,.9],False),([.4,.3],False),([.8,.79],False)):
                    body = response([{'index':i,'relevance_score':v} for i,v in enumerate(values)])
                    def opener(request, **_):
                        self.assertEqual(request.full_url, 'http://127.0.0.1:8083/v1/rerank')
                        self.assertEqual(len(json.loads(request.data)['documents']),2)
                        return io.BytesIO(json.dumps(body).encode())
                    self.assertEqual(service.healthy(self.config(), opener), expected)
                def unavailable(*_, **__):raise RuntimeError('private-upstream-body')
                self.assertFalse(service.healthy(self.config(), unavailable))


class PassageTests(unittest.TestCase):
    def test_selects_relevant_source_chunk_with_attribution(self):
        source = {'title':'Guild guides','text':[{'text_chunk':'The guild roster lists clerics and warriors.'},
                  {'text_chunk':'Monk weight limits affect armor class.','title':'Monk guide','context':'Equipment',
                   'source_start':41,'source_end':82}]}
        before=deepcopy(source);chosen=pilot.passages('monk weight',source,CharacterTokenizer())
        self.assertEqual(chosen[0]['passage_index'],1)
        self.assertIn('Monk guide\nEquipment',chosen[0]['document'])
        self.assertEqual(chosen[0]['source_start'],41)
        self.assertEqual(chosen[0]['source_end'],82)
        self.assertEqual(source,before)

    def test_complete_prompt_budget_counts_query_heading_and_wrapper(self):
        chosen=pilot.passages('monk weight',{'title':'x'*200,'text':[{'text_chunk':'monk weight '+'a'*3000}]},CharacterTokenizer())
        self.assertTrue(chosen[0]['shortened'])
        self.assertLessEqual(chosen[0]['tokens'],512)
        self.assertLess(len(chosen[0]['document']),512)
        with self.assertRaises(ValueError):pilot.passages('x'*500,{'text_full':'body'},CharacterTokenizer())

    def test_empty_sources_keep_their_baseline_position_and_ocr_is_explicit(self):
        self.assertEqual(pilot.passages('query',{},CharacterTokenizer()),[])
        chosen=pilot.passages('query',{'llm_image_text_full':'OCR source text'},CharacterTokenizer())
        self.assertEqual(chosen[0]['selector'],'reader_or_ocr_prefix')
        hits=[{'_id':'a'},{'_id':'image'},{'_id':'b'}]
        result=pilot.rerank(hits,{'a':.1,'b':.9})
        self.assertEqual([h['_id'] for h in result],['b','image','a'])
        self.assertEqual(hits,[{'_id':'a'},{'_id':'image'},{'_id':'b'}])

    def test_identical_passages_are_not_scored_twice_and_ties_stay_stable(self):
        chosen=pilot.passages('weight',{'text':[{'text_chunk':'monk weight'}]*2},CharacterTokenizer(),count=2)
        self.assertEqual(len(chosen),1)
        hits=[{'_id':'b'},{'_id':'a'}]
        self.assertEqual([h['_id'] for h in pilot.rerank(hits,{'a':.5,'b':.5})],['b','a'])
        with self.assertRaises(ValueError):pilot.rerank(hits,{'unknown':.5})
        with self.assertRaises(ValueError):pilot.rerank(hits,{'a':float('inf')})
        with self.assertRaises(ValueError):pilot.rerank([{'_id':'a'},{'_id':'a'}],{'a':.5})


class ClientTests(unittest.TestCase):
    def test_pilot_does_not_send_credentials_to_nonlocal_endpoints(self):
        for url in ('https://example.com','http://127.0.0.1.evil:8083','http://user:secret@127.0.0.1',
                    'http://127.0.0.1/path','http://127.0.0.1?redirect=1','http://[::1]:8083'):
            with self.subTest(url=url), self.assertRaises(ValueError):pilot.Client(url,'key')

    def test_request_and_response_limits_and_complete_indexed_results(self):
        client=pilot.Client('http://127.0.0.1:8083','private-test-key')
        with self.assertRaises(ValueError):client.rank('query',[])
        with self.assertRaises(ValueError):client.rank('query',['x']*101)
        with self.assertRaises(ValueError):client.rank('query',['x'*(1024*1024)])
        valid=response([{'index':1,'relevance_score':.9},{'index':0,'relevance_score':.1}])
        opener=SimpleNamespace(open=lambda *args,**kwargs:io.BytesIO(json.dumps(valid).encode()))
        with patch('urllib.request.build_opener',return_value=opener):
            values,elapsed,_=client.rank('query',['one','two'])
            self.assertEqual(values,[.1,.9]);self.assertGreaterEqual(elapsed,0)
        opener.open=lambda *args,**kwargs:io.BytesIO(b'x'*(128*1024+1))
        with patch('urllib.request.build_opener',return_value=opener),self.assertRaisesRegex(ValueError,'byte limit'):
            client.rank('query',['one'])


if __name__=='__main__':unittest.main()
