import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('checks', Path(__file__).with_name('check-embeddings.py'))
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class MigrationChecks(unittest.TestCase):
    def test_first_deployment_accepts_only_verified_zero_checkpoints(self):
        empty = {'hits': {'hits': []}}
        with patch.object(checks, 'post', side_effect=[empty, {'hits': {'total': {'value': 0, 'relation': 'eq'}}}]) as post:
            checks.check_search('http://fixture', [1.], 'embeddinggemma-2-768-v1')
            self.assertEqual(post.call_args_list[0].args[1]['knn']['filter'],
                             {'term': {'embedding_model': 'embeddinggemma-2-768-v1'}})
        for response in ({'hits': {'total': {'value': 1, 'relation': 'eq'}}}, {'timed_out': True}, {}):
            with patch.object(checks, 'post', side_effect=[empty, response]), self.assertRaises(AssertionError):
                checks.check_search('http://fixture', [1.], 'embeddinggemma-2-768-v1')

    def test_incomplete_or_legacy_empty_results_fail(self):
        for result in ({'hits': {'hits': []}}, {'error': 'failed'}, {'timed_out': True}, {'_shards': {'failed': 1}}):
            with patch.object(checks, 'post', return_value=result), self.assertRaises(AssertionError):
                checks.check_search('http://fixture', [1.], 'old-model')
        with patch.object(checks, 'post', return_value={'hits': {'hits': [{'_id': 'ready'}]}}):
            checks.check_search('http://fixture', [1.], 'embeddinggemma-2-768-v1')


if __name__ == '__main__':
    unittest.main()
