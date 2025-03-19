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
        self.assertEqual(finder._REINDEXING_INTERVAL, 2628000)

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

        for p in ["path1", "path2"]:
            mock_subprocess.assert_any_call([
                "git", "-C", finder._LOCAL_REPO_PATH,
                "sparse-checkout", "add", p
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
    def test_check_and_queue_file_recently_indexed(self, mock_datetime):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        finder._REINDEXING_ENABLED = True
        finder._REINDEXING_INTERVAL = 3600

        now = datetime.now(timezone.utc)
        last_indexed = now - timedelta(seconds=1800)  # Within interval

        es_manager.record_exists.return_value = True
        doc = {'_source': {'last_indexed': last_indexed.isoformat(), 'llm_summary': 'summary'}}
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

class TestCheckAndQueueFileLLMEnrichment(unittest.TestCase):
    def test_skip_llm_enrichment_true_bypasses_enrichment_check(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager, skip_llm_enrichment=True)
        finder._REINDEXING_ENABLED = False
        
        es_manager.record_exists.return_value = True
        
        # Document is indexed but not enriched
        doc = {'_source': {'last_indexed': datetime.now(timezone.utc).isoformat()}}
        es_manager.get_document.return_value = doc
        
        finder._check_and_queue_file('test/path')
        
        # File should not be queued since enrichment check is skipped and reindexing is disabled
        rabbitmq_manager.publish_message.assert_not_called()

    def test_file_with_missing_llm_summary_is_queued(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        
        es_manager.record_exists.return_value = True
        
        # Document is indexed but has no llm_summary field
        doc = {'_source': {'last_indexed': datetime.now(timezone.utc).isoformat()}}
        es_manager.get_document.return_value = doc
        
        finder._check_and_queue_file('test/path')
        
        # File should be queued for enrichment
        rabbitmq_manager.publish_message.assert_called_once_with(item={"file_path": "test/path"})

    def test_file_with_placeholder_llm_summary_is_queued(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        
        es_manager.record_exists.return_value = True
        
        # Document is indexed but has placeholder llm_summary
        doc = {
            '_source': {
                'last_indexed': datetime.now(timezone.utc).isoformat(),
                'llm_summary': "[ Still awaiting LLM Enrichment... ]"
            }
        }
        es_manager.get_document.return_value = doc
        
        finder._check_and_queue_file('test/path')
        
        # File should be queued for enrichment
        rabbitmq_manager.publish_message.assert_called_once_with(item={"file_path": "test/path"})

    def test_enriched_file_with_always_enrich_true_is_queued(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager, always_do_llm_enrichment=True)
        
        es_manager.record_exists.return_value = True
        
        # Document is indexed and has a valid llm_summary
        doc = {
            '_source': {
                'last_indexed': datetime.now(timezone.utc).isoformat(),
                'llm_summary': "This is a proper summary."
            }
        }
        es_manager.get_document.return_value = doc
        
        finder._check_and_queue_file('test/path')
        
        # File should be queued for re-enrichment
        rabbitmq_manager.publish_message.assert_called_once_with(item={"file_path": "test/path"})

    def test_enriched_file_not_queued_when_already_enriched(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        finder._REINDEXING_ENABLED = False
        
        es_manager.record_exists.return_value = True
        
        # Document is indexed and has a valid llm_summary
        doc = {
            '_source': {
                'last_indexed': datetime.now(timezone.utc).isoformat(),
                'llm_summary': "This is a proper summary."
            }
        }
        es_manager.get_document.return_value = doc
        
        finder._check_and_queue_file('test/path')
        
        # File should not be queued since it's already enriched and reindexing is disabled
        rabbitmq_manager.publish_message.assert_not_called()

class TestShouldSkipFile(unittest.TestCase):
    def setUp(self):
        # Create a FileFinder instance with dummy managers and a mock logger
        self.es_manager = MagicMock()
        self.rabbitmq_manager = MagicMock()
        self.mock_logger = MagicMock()
        self.finder = FileFinder(self.es_manager, self.rabbitmq_manager, logger=self.mock_logger)
    
    def test_hidden_file(self):
        # Files starting with a dot should be skipped.
        filename = ".hidden.txt"
        full_path = "/some/path/.hidden.txt"
        relative_path = ".hidden.txt"
        result = self.finder._should_skip_file(filename, full_path, relative_path)
        self.assertTrue(result)
        self.mock_logger.debug.assert_called_with("Skipping hidden file: .hidden.txt")
        
    def test_mailing_lists_non_json(self):
        # In mailing-lists directory, non-json files should be skipped.
        filename = "file.txt"
        full_path = "/data/mailing-lists/file.txt"
        relative_path = "mailing-lists/file.txt"
        result = self.finder._should_skip_file(filename, full_path, relative_path)
        self.assertTrue(result)
        self.mock_logger.debug.assert_called_with("Skipping non-json mailing-list file: mailing-lists/file.txt")
    
    def test_mailing_lists_json(self):
        # In mailing-lists directory, .json files should not be skipped.
        filename = "file.json"
        full_path = "/data/mailing-lists/file.json"
        relative_path = "mailing-lists/file.json"
        result = self.finder._should_skip_file(filename, full_path, relative_path)
        self.assertFalse(result)
    
    def test_newsgroups_non_txt(self):
        # In newsgroups directory, non-txt files should be skipped.
        filename = "file.html"
        full_path = "/data/newsgroups/file.html"
        relative_path = "newsgroups/file.html"
        result = self.finder._should_skip_file(filename, full_path, relative_path)
        self.assertTrue(result)
        self.mock_logger.debug.assert_called_with("Skipping non-txt newsgroups file: newsgroups/file.html")
    
    def test_newsgroups_txt(self):
        # In newsgroups directory, .txt files should not be skipped.
        filename = "file.txt"
        full_path = "/data/newsgroups/file.txt"
        relative_path = "newsgroups/file.txt"
        result = self.finder._should_skip_file(filename, full_path, relative_path)
        self.assertFalse(result)
    
    def test_non_special_file(self):
        # Files that are not hidden and don't belong to mailing-lists or newsgroups should not be skipped.
        filename = "document.pdf"
        full_path = "/data/others/document.pdf"
        relative_path = "others/document.pdf"
        result = self.finder._should_skip_file(filename, full_path, relative_path)
        self.assertFalse(result)

class TestLLMEnrichmentFlags(unittest.TestCase):
    def test_skip_llm_enrichment_default(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        
        finder = FileFinder(es_manager, rabbitmq_manager)
        
        self.assertFalse(finder._SKIP_LLM_ENRICHMENT)
        self.assertFalse(finder._ALWAYS_DO_LLM_ENRICHMENT)
        
    def test_skip_llm_enrichment_constructor_param(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        
        finder = FileFinder(es_manager, rabbitmq_manager, skip_llm_enrichment=True)
        
        self.assertTrue(finder._SKIP_LLM_ENRICHMENT)
        self.assertFalse(finder._ALWAYS_DO_LLM_ENRICHMENT)
        
    def test_always_do_llm_enrichment_constructor_param(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        
        finder = FileFinder(es_manager, rabbitmq_manager, always_do_llm_enrichment=True)
        
        self.assertFalse(finder._SKIP_LLM_ENRICHMENT)
        self.assertTrue(finder._ALWAYS_DO_LLM_ENRICHMENT)
        
    @patch.dict('os.environ', {'SKIP_LLM_ENRICHMENT': 'true'})
    def test_skip_llm_enrichment_env_var(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        
        finder = FileFinder(es_manager, rabbitmq_manager)
        
        self.assertTrue(finder._SKIP_LLM_ENRICHMENT)
        
    @patch.dict('os.environ', {'ALWAYS_DO_LLM_ENRICHMENT': 'true'})
    def test_always_do_llm_enrichment_env_var(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        
        finder = FileFinder(es_manager, rabbitmq_manager)
        
        self.assertTrue(finder._ALWAYS_DO_LLM_ENRICHMENT)
        
    @patch.dict('os.environ', {'SKIP_LLM_ENRICHMENT': 'true'})
    def test_env_var_overrides_constructor_param(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        
        finder = FileFinder(es_manager, rabbitmq_manager, skip_llm_enrichment=False)
        
        self.assertTrue(finder._SKIP_LLM_ENRICHMENT)
        
    @patch.dict('os.environ', {'SKIP_LLM_ENRICHMENT': 'false'})
    def test_skip_llm_enrichment_env_var_false(self):
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        
        finder = FileFinder(es_manager, rabbitmq_manager, skip_llm_enrichment=True)
        
        self.assertFalse(finder._SKIP_LLM_ENRICHMENT)

class TestCacheTimestamp(unittest.TestCase):
    def setUp(self):
        self.es_manager = MagicMock()
        self.rabbitmq_manager = MagicMock()
        self.finder = FileFinder(self.es_manager, self.rabbitmq_manager)
        self.test_file = "test/file.txt"
        
    @patch('datetime.datetime')
    def test_file_not_in_cache_gets_queued(self, mock_datetime):
        # Setup
        now = datetime(2025, 3, 18, tzinfo=timezone.utc)
        mock_datetime.now.return_value = now
        self.es_manager.record_exists.return_value = False
        
        # Test
        self.finder._check_and_queue_file(self.test_file)
        
        # Verify
        self.rabbitmq_manager.publish_message.assert_called_once_with(item={"file_path": self.test_file})
        self.assertIn(self.test_file, self.finder._enqueued_cache)
        self.assertGreaterEqual(self.finder._enqueued_cache[self.test_file], int(now.timestamp()))
        
    @patch('datetime.datetime')
    def test_recently_cached_file_skipped(self, mock_datetime):
        # Setup
        now = datetime(2025, 3, 18, tzinfo=timezone.utc)
        mock_datetime.now.return_value = now
        
        # Simulate a file that was recently cached (10 minutes ago)
        ten_min_ago = int((now - timedelta(minutes=10)).timestamp())
        self.finder._enqueued_cache[self.test_file] = ten_min_ago
        
        # Test
        self.finder._check_and_queue_file(self.test_file)
        
        # Verify no publish happened
        self.rabbitmq_manager.publish_message.assert_not_called()
        
    @patch('datetime.datetime')
    def test_old_cached_file_gets_requeued(self, mock_datetime):
        # Setup
        now = datetime(2025, 3, 18, tzinfo=timezone.utc)
        mock_datetime.now.return_value = now
        self.es_manager.record_exists.return_value = False
        
        # Simulate a file that was cached longer ago than the reindex interval
        old_timestamp = int((now - timedelta(seconds=self.finder._REINDEXING_INTERVAL + 100)).timestamp())
        self.finder._enqueued_cache[self.test_file] = old_timestamp
        
        # Test
        self.finder._check_and_queue_file(self.test_file)
        
        # Verify
        self.rabbitmq_manager.publish_message.assert_called_once_with(item={"file_path": self.test_file})
        self.assertIn(self.test_file, self.finder._enqueued_cache)
        self.assertGreaterEqual(self.finder._enqueued_cache[self.test_file], int(now.timestamp()))
        
    @patch('datetime.datetime')
    def test_cache_updated_when_file_queued(self, mock_datetime):
        # Setup
        now = datetime(2025, 3, 18, tzinfo=timezone.utc)
        mock_datetime.now.return_value = now
        self.es_manager.record_exists.return_value = False
        
        # Test
        self.finder._check_and_queue_file(self.test_file)
        
        # Verify cache was updated
        self.assertGreaterEqual(self.finder._enqueued_cache[self.test_file], int(now.timestamp()))
        
    @patch('datetime.datetime')
    def test_cache_respects_reindexing_interval(self, mock_datetime):
        # Setup
        now = datetime(2025, 3, 18, tzinfo=timezone.utc)
        mock_datetime.now.return_value = now
        self.es_manager.record_exists.return_value = False
        
        # Set a custom reindexing interval
        custom_interval = 7200  # 2 hours
        self.finder._REINDEXING_INTERVAL = custom_interval
        
        # Simulate a file cached exactly at the boundary
        boundary_timestamp = int((now - timedelta(seconds=custom_interval)).timestamp())
        self.finder._enqueued_cache[self.test_file] = boundary_timestamp
        
        # Test
        self.finder._check_and_queue_file(self.test_file)
        
        # Verify file was queued (since it's exactly at the boundary)
        self.rabbitmq_manager.publish_message.assert_called_once_with(item={"file_path": self.test_file})

class TestBatchedFutureProcessing(unittest.TestCase):
    @patch.object(FileFinder, '_sparse_checkout_repo')
    @patch('os.walk')
    def test_max_pending_futures_respected(self, mock_walk, mock_checkout):
        # Setup
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager, max_workers=2)
        
        # Override the _process_file method to add delay and track calls
        original_process_file = finder._process_file
        process_calls = []
        
        def slow_process_file(path):
            process_calls.append(path)
            # Call the original but add tracking
            original_process_file(path)
            
        finder._process_file = slow_process_file
        
        # Prepare test data - 10 files
        mock_walk.return_value = [
            (finder._LOCAL_REPO_PATH, ['dir1'], ['file1.txt', 'file2.txt', 'file3.txt', 'file4.txt', 'file5.txt',
                                'file6.txt', 'file7.txt', 'file8.txt', 'file9.txt', 'file10.txt']),
        ]
        
        # Set a small max_pending_futures for testing
        finder._max_workers = 2
        
        # Mock _process_completed_futures to track calls
        original_process_completed = finder._process_completed_futures
        process_completed_calls = []
        
        def mock_process_completed(active_futures):
            process_completed_calls.append(len(active_futures))
            original_process_completed(active_futures)
            
        finder._process_completed_futures = mock_process_completed
        
        # Test
        finder.walk_and_queue()
        
        # Verify
        # Ensure all files were processed
        self.assertEqual(len(process_calls), 10)
        
        # Verify that _process_completed_futures was called when batch limit reached
        self.assertTrue(any(count >= finder._max_workers * 3 for count in process_completed_calls), 
                       f"Batching threshold never reached. Counts: {process_completed_calls}")

    @patch.object(FileFinder, '_sparse_checkout_repo')
    @patch('os.walk')
    @patch('concurrent.futures.wait')
    def test_process_completed_futures(self, mock_wait, mock_walk, mock_checkout):
        # Setup
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        
        # Create mock futures
        future1 = MagicMock()
        future2 = MagicMock()
        future3 = MagicMock()
        
        active_futures = [future1, future2, future3]
        
        # Configure mock_wait to return done futures
        mock_wait.return_value = ([future1, future2], [future3])
        
        # Test
        finder._process_completed_futures(active_futures)
        
        # Verify
        mock_wait.assert_called_once()
        future1.result.assert_called_once()
        future2.result.assert_called_once()
        future3.result.assert_not_called()
        
        # Check that completed futures were removed
        self.assertEqual(len(active_futures), 1)
        self.assertIn(future3, active_futures)

    @patch.object(FileFinder, '_sparse_checkout_repo')
    @patch('os.walk')
    def test_exception_in_worker_thread(self, mock_walk, mock_checkout):
        # Setup
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        mock_logger = MagicMock()
        finder._logger = mock_logger
        
        # Override the _process_file method to raise an exception
        def failing_process_file(path):
            if path == "file2.txt":
                raise ValueError("Test error")
                
        finder._process_file = failing_process_file
        
        # Prepare test data
        mock_walk.return_value = [
            (finder._LOCAL_REPO_PATH, [], ['file1.txt', 'file2.txt', 'file3.txt']),
        ]
        
        # Test
        finder.walk_and_queue()
        
        # Verify
        # Check that the error was logged
        error_calls = [call for call in mock_logger.error.call_args_list if "Test error" in str(call)]
        self.assertGreaterEqual(len(error_calls), 1)

    @patch.object(FileFinder, '_sparse_checkout_repo')
    @patch('os.walk')
    def test_all_files_eventually_processed(self, mock_walk, mock_checkout):
        # Setup
        es_manager = MagicMock()
        rabbitmq_manager = MagicMock()
        finder = FileFinder(es_manager, rabbitmq_manager)
        
        # Track processed files
        processed_files = []
        def track_process(path):
            processed_files.append(path)
            
        finder._process_file = track_process
        
        # Prepare test data - multiple directories
        mock_walk.return_value = [
            (finder._LOCAL_REPO_PATH, ['dir1'], ['file1.txt', 'file2.txt']),
            (finder._LOCAL_REPO_PATH + '/dir1', [], ['file3.txt', 'file4.txt']),
        ]
        
        # Test
        finder.walk_and_queue()
        
        # Verify all files were processed - normalize paths before comparison
        expected_files = ["file1.txt", "file2.txt", "dir1/file3.txt", "dir1/file4.txt"]
        self.assertEqual(sorted(processed_files), sorted(expected_files))

if __name__ == '__main__':
    unittest.main()

