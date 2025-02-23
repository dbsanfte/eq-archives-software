import os
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from .es_manager import ElasticsearchManager
from .rabbitmq_manager import RabbitMQManager

class FileFinder:
    def __init__(self, es_manager: ElasticsearchManager, rabbitmq_manager: RabbitMQManager, logger=None):
        self._es_manager = es_manager
        self._rabbitmq_manager = rabbitmq_manager
        self._logger = logger or logging.getLogger(__name__)
        
        self._GIT_REPO_URL = os.environ.get("GIT_REPO_URL", "https://github.com/dbsanfte/eq-archives.git")
        self._LOCAL_REPO_PATH = os.environ.get("LOCAL_REPO_PATH", "/data/eq-archives")
        self._SPARSE_CHECKOUT_PATHS = os.environ.get("SPARSE_CHECKOUT_PATHS", "")
        self._REINDEXING_ENABLED = os.environ.get("REINDEXING_ENABLED", "false").lower() == "true"
        self._REINDEXING_INTERVAL = int(os.environ.get("REINDEXING_INTERVAL", "86400"))
        # In-memory cache for tracking enqueued files: {relative_path: timestamp}
        self._enqueued_cache = {}
        
    def _sparse_checkout_repo(self):
        if not os.path.exists(f"{self._LOCAL_REPO_PATH}/.git"):
            self._logger.info(f"Cloning repo into {self._LOCAL_REPO_PATH}")
            os.makedirs(self._LOCAL_REPO_PATH, exist_ok=True)
            
            subprocess.run([
                "git", "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--depth", "1",
                "--sparse",
                self._GIT_REPO_URL,
                self._LOCAL_REPO_PATH
            ], check=True)
        
        subprocess.call([
            "rm", "-f", 
            f"{self._LOCAL_REPO_PATH}/.git/index.lock",
            f"{self._LOCAL_REPO_PATH}/.git/info/sparse-checkout.lock"
        ])
        
        if self._SPARSE_CHECKOUT_PATHS.strip():
            # Check existing sparse-checkout paths
            result = subprocess.run([
                "git", "-C", self._LOCAL_REPO_PATH, "sparse-checkout", "list"
            ], capture_output=True, text=True, check=True)
            current_paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            
            for p in self._SPARSE_CHECKOUT_PATHS.split(","):
                if p not in current_paths:
                    self._logger.info(f"Adding sparse checkout path: {p}")
                    subprocess.run([
                        "git", "-C", self._LOCAL_REPO_PATH, "sparse-checkout", "add", p
                    ], check=True)
                else:
                    self._logger.info(f"Sparse checkout path already present: {p}")
        subprocess.run(["git", "-C", self._LOCAL_REPO_PATH, "checkout"], check=True)
        subprocess.run(["git", "-C", self._LOCAL_REPO_PATH, "pull"], check=True)

    def _should_skip_file(self, filename, full_path, relative_path):
        if filename.startswith("."):
            self._logger.debug(f"Skipping hidden file: {filename}")
            return True
        if "mailing-lists/" in full_path and not filename.endswith(".html"):
            self._logger.debug(f"Skipping non-html mailing-list file: {relative_path}")
            return True
        if "newsgroups/" in full_path and not filename.endswith(".txt"):
            self._logger.debug(f"Skipping non-txt newsgroups file: {relative_path}")
            return True
        return False

    def _check_and_queue_file(self, relative_path: str):
        """
        Revised to use an in-memory cache to prevent flooding the queue with duplicate messages.
        The cache entry expires after REINDEXING_INTERVAL seconds.
        """
        self._logger.debug(f"Checking file: {relative_path}")
        now = datetime.now(timezone.utc)
        
        # Check local cache for recent enqueue
        cache_timestamp = self._enqueued_cache.get(relative_path)
        if cache_timestamp and (now - cache_timestamp).total_seconds() < self._REINDEXING_INTERVAL:
            self._logger.debug(f"File {relative_path} was recently queued at {cache_timestamp}. Skipping duplicate enqueue.")
            return
        
        chunk_prefix = f"{relative_path}#"
        if self._es_manager.record_exists(doc_id=relative_path) or self._es_manager.chunks_exist(prefix=chunk_prefix):
            if not self._REINDEXING_ENABLED:
                self._logger.debug(f"Already indexed or chunked: {relative_path}")
                return
            else:
                doc = self._es_manager.get_document(doc_id=relative_path) or {}
                last_indexed_str = doc.get("_source", {}).get("last_indexed")
                if last_indexed_str:
                    last_indexed = datetime.fromisoformat(last_indexed_str).replace(tzinfo=timezone.utc)
                    if last_indexed >= now - timedelta(seconds=self._REINDEXING_INTERVAL):
                        self._logger.debug(f"Reindexing enabled, but file was indexed recently: {relative_path}")
                        return
                self._logger.debug(f"Reindexing enabled, and file is due for reindexing: {relative_path}")
        
        self._logger.info(f"Queueing file for indexing: {relative_path}")
        msg = {"file_path": relative_path}
        self._rabbitmq_manager.publish_message(item=msg)
        
        # Update the cache with the current timestamp for this file
        self._enqueued_cache[relative_path] = now
        
    def walk_and_queue(self):
        self._sparse_checkout_repo()
        for root, dirs, files in os.walk(self._LOCAL_REPO_PATH):
            if ".git" in dirs:
                dirs.remove(".git")
            
            self._logger.info(f"Processing files in {root}...")
            self._logger.debug(f"Number of files found: {len(files)}")
            
            for filename in files:
                full_path = os.path.join(root, filename).replace(os.path.sep, "/")
                relative_path = os.path.relpath(full_path, self._LOCAL_REPO_PATH).replace(os.path.sep, "/")
                
                if self._should_skip_file(filename, full_path, relative_path):
                    continue
                    
                self._logger.debug(f"Processing file: {relative_path}")
                try:
                    self._check_and_queue_file(relative_path)
                except Exception as e:
                    self._logger.error(f"Error processing file: {relative_path}: {e}")
                    self._logger.exception(e)