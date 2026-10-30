import copy
import io
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import render
import runner


def predecessor():
    return {'metadata': {'name': 'old-reindex', 'namespace': 'eqarchives-es', 'uid': 'old-uid'},
            'status': {'conditions': [{'type': 'Failed', 'status': 'True'}]},
            'spec': {'selector': {'matchLabels': {'controller-uid': 'old-uid'}},
                     'template': {'metadata': {'labels': {'job-name': 'old-reindex'}}, 'spec': {
                         'restartPolicy': 'Never', 'initContainers': [{'name': 'prepare-sources'}],
                         'volumes': [{'name': 'archive', 'hostPath': {
                             'path': '/mnt/samsung/eq-archives-html-reindex'}}],
                         'containers': [{'name': 'indexer', 'image': 'immutable-original-image',
                             'command': ['python', '-u', 'reindex_text.pyc'],
                             'env': [{'name': 'SKIP_LLM_ENRICHMENT', 'value': 'true'},
                                     {'name': 'NOMIC_EMBEDDING_BATCH_SIZE', 'value': '2'}],
                             'volumeMounts': [{'name': 'archive', 'readOnly': True}]}]}}}}


class RecoveryTests(unittest.TestCase):
    def test_repeated_upstream_failures_retain_the_original_error_classification(self):
        class Transport(Exception): pass
        class Connection(Exception): pass
        class Server(Exception): pass
        class Rejected(Exception): pass
        class ApiError(Exception):
            status_code = 503
        modules = {'elastic_transport': SimpleNamespace(TransportError=Transport),
                   'elasticsearch': SimpleNamespace(ApiError=ApiError),
                   'openai': SimpleNamespace(APIConnectionError=Connection, APITimeoutError=Connection,
                                            InternalServerError=Server, RateLimitError=Server)}
        with patch.dict('sys.modules', modules):
            for original, expected in [(Transport(), True), (Connection(), True), (Server(), True),
                                       (ApiError(), True), (Rejected(), False), (ValueError(), False)]:
                try:
                    try: raise original
                    except Exception: raise RuntimeError(runner.UPSTREAM_FAILURE) from None
                except RuntimeError as error:
                    self.assertEqual(runner.is_transient(error), expected)
            self.assertFalse(runner.is_transient(RuntimeError(runner.UPSTREAM_FAILURE)))

    def test_mounted_runner_preserves_sibling_imports_and_exact_id_arguments(self):
        import sys
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / 'recovery_test_sibling.py').write_text('value = 7')
            (path / 'worker.py').write_text('import recovery_test_sibling,sys\nassert recovery_test_sibling.value == 7\nassert sys.argv[1:] == ["--id", "exact-id"]\nraise SystemExit(0)')
            with patch.dict('os.environ', {'REINDEX_ENTRYPOINT': str(path / 'worker.py')}), \
                 patch.object(sys, 'argv', ['runner.py', '--id', 'exact-id']), patch.object(sys, 'path', list(sys.path)):
                self.assertEqual(runner.run(ready=lambda: True), 0)

    def test_dns_and_dependency_startup_waits_then_continues(self):
        readiness = iter([False] * 10 + [True])
        sleeps = []
        executed = []
        result = runner.run(ready=lambda: next(readiness), execute=lambda: executed.append('run'), sleep=sleeps.append)
        self.assertEqual(result, 0)
        self.assertEqual(executed, ['run'])
        self.assertEqual(sleeps, [5, 10, 20, 40] + [60] * 6)

    def test_multiple_outages_resume_committed_checkpoints_without_repeating_them(self):
        saved = []
        calls = []
        def execute():
            # The real reindex queries remaining version checkpoints each time.
            calls.append(1)
            saved.append(len(saved) + 1)
            if len(calls) <= 8:
                raise RuntimeError(runner.UPSTREAM_FAILURE)
            raise SystemExit(0)
        sleeps = []
        self.assertEqual(runner.run(ready=lambda: True, execute=execute,
            transient=lambda e: str(e) == runner.UPSTREAM_FAILURE, sleep=sleeps.append), 0)
        self.assertEqual(saved, list(range(1, 10)))
        self.assertEqual(len(sleeps), 8)

    def test_content_failures_never_loop_or_claim_success(self):
        for failure in (SystemExit(1), ValueError('Invalid source'), RuntimeError('Other failure')):
            with self.subTest(failure=failure):
                def execute(): raise failure
                sleeps = []
                if isinstance(failure, SystemExit):
                    self.assertEqual(runner.run(ready=lambda: True, execute=execute, sleep=sleeps.append), 1)
                else:
                    with self.assertRaises(type(failure)):
                        runner.run(ready=lambda: True, execute=execute, transient=lambda e: False, sleep=sleeps.append)
                self.assertEqual(sleeps, [])

    def test_readiness_distinguishes_connection_failures_from_bad_credentials(self):
        for error in (URLError('DNS not ready'), HTTPError('http://internal', 503, 'retry', {}, None)):
            def opener(*args, **kwargs): raise error
            self.assertFalse(runner.request_ready('http://internal', {}, opener=opener))
        for code in (400, 401, 403, 404):
            def opener(*args, **kwargs): raise HTTPError('http://private', code, 'secret payload', {}, None)
            with self.assertRaises(runner.ConfigurationError) as captured:
                runner.request_ready('http://internal', {}, opener=opener)
            self.assertNotIn('secret', str(captured.exception))
        class Response(io.StringIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        for status, ready in [('green', True), ('yellow', True), ('ok', True), ('red', False)]:
            self.assertEqual(runner.request_ready('http://internal', {},
                opener=lambda *a, **k: Response(json.dumps({'status': status}))), ready)
        for data, ready in [([{'id': 'nomic'}], True), ([], False), (None, False)]:
            self.assertEqual(runner.request_ready('http://internal/v1/models', {}, models=True,
                opener=lambda *a, **k: Response(json.dumps({'data': data}))), ready)
        def unauthorized(*args, **kwargs): raise HTTPError('http://model/v1/models', 401, 'bad key', {}, None)
        with self.assertRaises(runner.ConfigurationError):
            runner.request_ready('http://model/v1/models', {}, models=True, opener=unauthorized)

    def test_credentials_are_read_from_mounted_files_and_not_logged(self):
        calls = []
        with patch.dict('os.environ', {'ELASTICSEARCH_HOST': 'http://es', 'ELASTICSEARCH_PORT': '9200',
                                      'OPENAI_ENDPOINT': 'http://model:8080/v1'}), \
             patch.object(Path, 'read_text', return_value='private-key\n'), \
             patch.object(runner, 'request_ready', side_effect=lambda u, h, **k: calls.append(u) or True):
            self.assertTrue(runner.dependencies_ready())
        self.assertEqual(calls, ['http://es:9200/_cluster/health?wait_for_status=yellow&timeout=5s', 'http://model:8080/v1/models'])
        with patch.object(Path, 'read_text', side_effect=OSError('secret detail')):
            with self.assertRaises(runner.ConfigurationError): runner.dependencies_ready()
        with patch.object(Path, 'read_text', return_value=''):
            with self.assertRaises(runner.ConfigurationError): runner.dependencies_ready()
        with patch.object(runner, 'run', side_effect=runner.ConfigurationError('Invalid configuration')):
            self.assertEqual(runner.main(), 42)

    def test_continuation_preserves_image_sources_and_policy_and_removes_preparation(self):
        old = predecessor()
        saved = copy.deepcopy(old)
        result = render.render(old, 'reindex-resume-1', 'immutable runner')
        config, job = result['items']
        self.assertEqual(old, saved)
        self.assertTrue(config['immutable'])
        pod = job['spec']['template']['spec']
        self.assertNotIn('initContainers', pod)
        self.assertNotIn('metadata', job['spec']['template'])
        self.assertNotIn('selector', job['spec'])
        self.assertEqual(pod['containers'][0]['image'], 'immutable-original-image')
        self.assertTrue(pod['containers'][0]['volumeMounts'][0]['readOnly'])
        rules = job['spec']['podFailurePolicy']['rules']
        self.assertEqual(rules[0]['action'], 'FailJob')
        self.assertEqual(rules[1]['onPodConditions'][0]['type'], 'DisruptionTarget')
        self.assertEqual(rules[2]['onExitCodes']['values'], [143, 255])
        self.assertEqual(rules[2]['action'], 'Ignore')
        self.assertNotIn('activeDeadlineSeconds', job['spec'])

    def test_probes_have_only_the_requested_exact_id(self):
        result = render.render(predecessor(), 'probe-1', 'code', probe_id='websites/eq.example/20000101000000/index.html')
        self.assertEqual(result['items'][1]['spec']['template']['spec']['containers'][0]['args'],
                         ['--id', 'websites/eq.example/20000101000000/index.html'])
        with self.assertRaises(ValueError):
            render.render(predecessor(), 'probe-2', 'code', probe_id='websites/../private')

    def test_render_rejects_active_jobs_or_changed_safety_settings(self):
        variants = []
        for key, value in [('status', {}), ('metadata', {**predecessor()['metadata'], 'name': 'new-job'})]:
            old = predecessor(); old[key] = value; variants.append(old)
        for field, value in [('command', ['python', 'other.py']), ('args', ['--limit', '1'])]:
            old = predecessor(); old['spec']['template']['spec']['containers'][0][field] = value; variants.append(old)
        old = predecessor(); old['spec']['template']['spec']['containers'][0]['env'][0]['value'] = 'false'; variants.append(old)
        old = predecessor(); old['spec']['template']['spec']['containers'][0]['volumeMounts'][0]['readOnly'] = False; variants.append(old)
        for old in variants:
            with self.assertRaises(ValueError): render.render(old, 'new-job', 'code')
        for name in ('bad_name', 'a' * 64, 'x'):
            with self.assertRaises(ValueError): render.render(predecessor(), name, 'code')


if __name__ == '__main__':
    unittest.main()
