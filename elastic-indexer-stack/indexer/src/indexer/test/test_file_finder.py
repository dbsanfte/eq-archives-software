import unittest
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone, timedelta
from indexer.file_finder import FileFinder

class TestFileFinderInitialization(unittest.TestCase):
    @patch.dict('os.environ', {
        'GIT_REPO_URL': 'https://test.git',
        'LOCAL_REPO_PATH': '/test/path',
        'SPARSE_CHECKOUT_PATHS': 'path1,path2',
        'REINDEXING_ENABLED': 'true',
        'REINDEXING_INTERVAL': '3600'
    })
    def test_initialization_with_env_vars(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        logger = MagicMock()

        finder = FileFinder(es_manager, rabbitmq_manager, logger)

        self.assertEqual(finder._GIT_REPO_URL, 'https://test.git')
        self.assertEqual(finder._LOCAL_REPO_PATH, '/test/path')
        self.assertEqual(finder._SPARSE_CHECKOUT_PATHS, 'path1,path2')
        self.assertTrue(finder._REINDEXING_ENABLED)
        self.assertEqual(finder._REINDEXING_INTERVAL, 3600)

    def test_initialization_with_defaults(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()

        logger = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager, logger=logger)

        self.assertEqual(finder._GIT_REPO_URL, 'https://github.com/dbsanfte/eq-archives.git')
        self.assertEqual(finder._LOCAL_REPO_PATH, '/data/eq-archives')
        self.assertEqual(finder._SPARSE_CHECKOUT_PATHS, '')
        self.assertFalse(finder._REINDEXING_ENABLED)
        self.assertEqual(finder._REINDEXING_INTERVAL, 86400)

    def test_logger_initialization(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        custom_logger = MagicMock()
        
        finder = FileFinder(es_manager, rabbitmq_manager, custom_logger)
        
        self.assertIsNotNone(finder._logger)
        self.assertIs(finder._logger, custom_logger)
        self.assertIsNotNone(finder._es_manager)
        self.assertIs(finder._es_manager, es_manager)
        self.assertIsNotNone(finder._rabbitmq_manager)
        self.assertIs(finder._rabbitmq_manager, rabbitmq_manager)

class TestSparseCheckoutRepo(unittest.TestCase):
    @patch('subprocess.run')
    @patch('os.path.exists', return_value=False)
    @patch('os.makedirs')
    def test_clone_repository(self, mock_makedirs, mock_exists, mock_subprocess):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)

        finder._sparse_checkout_repo()

        mock_makedirs.assert_called_once_with(finder._LOCAL_REPO_PATH, exist_ok=True)
        mock_subprocess.assert_any_call([
            "git", "clone",
            "--filter=blob:none",
            "--no-checkout",
            "--depth", "1",
            "--sparse",
            finder._GIT_REPO_URL,
            finder._LOCAL_REPO_PATH
        ], check=True)

    @patch('subprocess.run')
    @patch('os.path.exists', return_value=True)
    def test_sparse_checkout_paths(self, mock_exists, mock_subprocess):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        finder._SPARSE_CHECKOUT_PATHS = "path1,path2"

        finder._sparse_checkout_repo()

        for path in ["path1", "path2"]:
            mock_subprocess.assert_any_call([
                "git", "-C", finder._LOCAL_REPO_PATH,
                "sparse-checkout", "add", path
            ], check=True)

    @patch('subprocess.run')
    @patch('os.path.exists', return_value=True)
    def test_update_repository(self, mock_exists, mock_subprocess):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)

        finder._sparse_checkout_repo()

        mock_subprocess.assert_any_call(["git", "-C", finder._LOCAL_REPO_PATH, "pull"], check=True)

class TestWalkAndQueue(unittest.TestCase):
    
    @patch.object(FileFinder, '_sparse_checkout_repo')
    @patch('os.walk')
    @patch('indexer.file_finder.logging.getLogger')
    @patch.dict('os.environ', {
        'LOCAL_REPO_PATH': '/root',
        'SPARSE_CHECKOUT_PATHS': 'dir1',
    })
    def test_walk_and_queue(self, mock_get_logger, mock_walk, mock_checkout):
        es_manager = MagicMock()
        es_manager.record_exists.return_value = False
        es_manager.chunks_exist.return_value = False
        rabbitmq_manager = MagicMock()
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger
        finder = FileFinder(es_manager, rabbitmq_manager)
    
        mock_walk.return_value = [
            ('/root', ['dir1'], ['file1.txt']),
            ('/root/dir1', [], ['file2.html'])
        ]
    
        finder.walk_and_queue()
        self.assertEqual(mock_logger.info.call_count, 4)  # Processing each directory
        calls=[call(item={"file_path": "dir1/file2.html"}), 
               call(item={"file_path": "file1.txt"})]
        rabbitmq_manager.publish_message.assert_has_calls(calls, any_order=True)

    @patch.object(FileFinder, '_sparse_checkout_repo')
    @patch('os.walk')
    @patch.dict('os.environ', {
        'LOCAL_REPO_PATH': '/root',
        'SPARSE_CHECKOUT_PATHS': 'dir1',
    })
    def test_skip_hidden_files(self, mock_walk, mock_checkout):
        es_manager = MagicMock()
        es_manager.record_exists.return_value = False
        es_manager.chunks_exist.return_value = False
        rabbitmq_manager = MagicMock()
        
        finder = FileFinder(es_manager, rabbitmq_manager)

        mock_walk.return_value = [
            ('/root', ['dir1'], ['.hidden', 'file.txt'])
        ]

        finder.walk_and_queue()

        rabbitmq_manager.publish_message.assert_called_once_with(item={"file_path": "file.txt"})

    @patch.object(FileFinder, '_sparse_checkout_repo')
    @patch('os.walk')
    def test_skip_non_html_mailing_lists(self, mock_walk, mock_checkout):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)

        mock_walk.return_value = [
            ('/root/mailing-lists', [], ['file.txt']),
            ('/root/newsgroups', [], ['file.html'])
        ]

        finder.walk_and_queue()

        self.assertEqual(rabbitmq_manager.publish_message.call_count, 0)

    @patch.object(FileFinder, '_sparse_checkout_repo')
    @patch('os.walk', return_value=[])
    def test_walk_and_queue_calls_sparse_checkout_repo(self, mock_walk, mock_sparse_checkout_repo):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)

        finder.walk_and_queue()

        mock_sparse_checkout_repo.assert_called_once()

class TestCheckAndQueueFile(unittest.TestCase):
    @patch('datetime.datetime')
    def test_check_and_queue_file_exists_no_reindex(self, mock_datetime):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        finder._REINDEXING_ENABLED = False

        es_manager.record_exists.return_value = True
        es_manager.chunks_exist.return_value = False
        finder._check_and_queue_file('test/path')

        self.assertEqual(rabbitmq_manager.publish_message.call_count, 0)
        
    @patch('datetime.datetime')
    def test_check_and_queue_chunks_exist_no_reindex(self, mock_datetime):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        finder._REINDEXING_ENABLED = False

        es_manager.record_exists.return_value = False
        es_manager.chunks_exist.return_value = True
        finder._check_and_queue_file('test/path')

        self.assertEqual(rabbitmq_manager.publish_message.call_count, 0)

    @patch('datetime.datetime')
    def test_check_and_queue_file_recently_indexed(self, mock_datetime):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        finder._REINDEXING_ENABLED = True
        finder._REINDEXING_INTERVAL = 3600

        now = datetime.now(timezone.utc)
        last_indexed = now - timedelta(seconds=1800)  # Within interval

        es_manager.record_exists.return_value = True
        doc = {'_source': {'last_indexed': last_indexed.isoformat()}}
        es_manager.get_document.return_value = doc

        finder._check_and_queue_file('test/path')

        self.assertEqual(rabbitmq_manager.publish_message.call_count, 0)

    @patch('datetime.datetime')
    def test_check_and_queue_file_old_index(self, mock_datetime):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        finder._REINDEXING_ENABLED = True
        finder._REINDEXING_INTERVAL = 3600

        now = datetime.now(timezone.utc)
        last_indexed = now - timedelta(seconds=5400)  # Beyond interval

        es_manager.record_exists.return_value = True
        doc = {'_source': {'last_indexed': last_indexed.isoformat()}}
        es_manager.get_document.return_value = doc

        finder._check_and_queue_file('test/path')

        rabbitmq_manager.publish_message.assert_called_once_with(item={"file_path": "test/path"})

    def test_check_and_queue_file_not_exists(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)

        es_manager.record_exists.return_value = False
        es_manager.chunks_exist.return_value = False
        finder._check_and_queue_file('test/path')

        rabbitmq_manager.publish_message.assert_called_once_with(item={"file_path": "test/path"})

class TestSparseCheckoutRepo(unittest.TestCase):
    @patch('subprocess.run')
    @patch('os.makedirs')
    @patch('os.path.exists', return_value=False)
    @patch('subprocess.call')
    def test_clones_if_missing_git(
        self, mock_subprocess_call, mock_exists, mock_makedirs, mock_run
    ):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)

        finder._sparse_checkout_repo()

        mock_makedirs.assert_called_once_with(finder._LOCAL_REPO_PATH, exist_ok=True)
        mock_run.assert_any_call([
            "git", "clone",
            "--filter=blob:none",
            "--no-checkout",
            "--depth", "1",
            "--sparse",
            finder._GIT_REPO_URL,
            finder._LOCAL_REPO_PATH
        ], check=True)
        mock_subprocess_call.assert_called_with([
            "rm", "-f",
            f"{finder._LOCAL_REPO_PATH}/.git/index.lock",
            f"{finder._LOCAL_REPO_PATH}/.git/info/sparse-checkout.lock"
        ])

    @patch('subprocess.run')
    @patch('os.path.exists', return_value=True)
    @patch('subprocess.call')
    def test_no_clone_if_git_exists(
        self, mock_subprocess_call, mock_exists, mock_run
    ):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)

        finder._sparse_checkout_repo()

        # Ensure we never call git clone if .git exists
        clone_calls = [
            c for c in mock_run.call_args_list
            if 'clone' in c[0][0]
        ]
        self.assertEqual(len(clone_calls), 0)
        mock_subprocess_call.assert_called_with([
            "rm", "-f",
            f"{finder._LOCAL_REPO_PATH}/.git/index.lock",
            f"{finder._LOCAL_REPO_PATH}/.git/info/sparse-checkout.lock"
        ])

    @patch('subprocess.run')
    @patch('os.path.exists', return_value=True)
    @patch('subprocess.call')
    def test_sparse_checkout_empty_paths(
        self, mock_subprocess_call, mock_exists, mock_run
    ):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        finder._SPARSE_CHECKOUT_PATHS = ""

        finder._sparse_checkout_repo()

        add_calls = [
            c for c in mock_run.call_args_list
            if 'sparse-checkout' in c[0][0]
        ]
        # No sparse-checkout add calls should occur
        self.assertEqual(len(add_calls), 0)
        mock_run.assert_any_call(["git", "-C", finder._LOCAL_REPO_PATH, "checkout"], check=True)
        mock_run.assert_any_call(["git", "-C", finder._LOCAL_REPO_PATH, "pull"], check=True)

    @patch('subprocess.run')
    @patch('os.path.exists', return_value=True)
    @patch('subprocess.call')
    def test_sparse_checkout_multiple_paths(
        self, mock_subprocess_call, mock_exists, mock_run
    ):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        finder._SPARSE_CHECKOUT_PATHS = "path1,path2,path3"

        finder._sparse_checkout_repo()

        expected_calls = [
            call([
                "git", "-C", finder._LOCAL_REPO_PATH, "sparse-checkout", "add", "path1"
            ], check=True),
            call([
                "git", "-C", finder._LOCAL_REPO_PATH, "sparse-checkout", "add", "path2"
            ], check=True),
            call([
                "git", "-C", finder._LOCAL_REPO_PATH, "sparse-checkout", "add", "path3"
            ], check=True)
        ]
        # Verify all expected add calls
        for c in expected_calls:
            self.assertIn(c, mock_run.call_args_list)

    @patch('subprocess.run')
    @patch('os.path.exists', return_value=True)
    @patch('subprocess.call')
    def test_pull_is_called_after_checkout(
        self, mock_subprocess_call, mock_exists, mock_run
    ):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)

        finder._sparse_checkout_repo()

        checkout_call = call(["git", "-C", finder._LOCAL_REPO_PATH, "checkout"], check=True)
        pull_call = call(["git", "-C", finder._LOCAL_REPO_PATH, "pull"], check=True)
        self.assertIn(checkout_call, mock_run.call_args_list)
        self.assertIn(pull_call, mock_run.call_args_list)
