import os
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from .es_manager import ElasticsearchManager
from .rabbitmq_manager import RabbitMQManager
from .openai_manager import OpenAIManager
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import queue

class FileFinder:
    def __init__(self, es_manager: ElasticsearchManager, rabbitmq_manager: RabbitMQManager, logger=None, 
                 skip_llm_enrichment: bool=False,
                 always_do_llm_enrichment: bool=False,
                 reindexing_enabled: bool=False, 
                 reindexing_interval: int=2628000,
                 git_repo_url: str="https://github.com/dbsanfte/eq-archives.git",
                 local_repo_path: str="/data/eq-archives", 
                 sparse_checkout_paths: str="",
                 max_workers: int=multiprocessing.cpu_count()):
        self._es_manager = es_manager
        self._rabbitmq_manager = rabbitmq_manager
        self._logger = logger or logging.getLogger(__name__)
        
        # Flags and constants
        self._GIT_REPO_URL = git_repo_url
        if os.environ.get("GIT_REPO_URL", "") != "":
            self._GIT_REPO_URL = os.environ.get("GIT_REPO_URL")
        self._LOCAL_REPO_PATH = local_repo_path
        if os.environ.get("LOCAL_REPO_PATH", "") != "":
            self._LOCAL_REPO_PATH = os.environ.get("LOCAL_REPO_PATH")
        self._SPARSE_CHECKOUT_PATHS = sparse_checkout_paths
        if os.environ.get("SPARSE_CHECKOUT_PATHS", "") != "":
            self._SPARSE_CHECKOUT_PATHS = os.environ.get("SPARSE_CHECKOUT_PATHS")
        self._REINDEXING_ENABLED = reindexing_enabled
        if os.environ.get("REINDEXING_ENABLED", "") != "":
            self._REINDEXING_ENABLED = os.environ.get("REINDEXING_ENABLED").lower() == "true"
        self._REINDEXING_INTERVAL = reindexing_interval
        if os.environ.get("REINDEXING_INTERVAL", "") != "":
            self._REINDEXING_INTERVAL = int(os.environ.get("REINDEXING_INTERVAL"))
        self._SKIP_LLM_ENRICHMENT = skip_llm_enrichment
        if os.environ.get("SKIP_LLM_ENRICHMENT", "") != "":
            self._SKIP_LLM_ENRICHMENT = os.environ.get("SKIP_LLM_ENRICHMENT").lower() == "true"
        self._ALWAYS_DO_LLM_ENRICHMENT = always_do_llm_enrichment
        if os.environ.get("ALWAYS_DO_LLM_ENRICHMENT", "") != "":
            self._ALWAYS_DO_LLM_ENRICHMENT = os.environ.get("ALWAYS_DO_LLM_ENRICHMENT").lower() == "true"
        self._max_workers = max_workers
        if os.environ.get("MAX_WORKERS", "") != "":
            self._max_workers = int(os.environ.get("MAX_WORKERS"))
        # In-memory cache for tracking enqueued files: {relative_path: timestamp}
        self._enqueued_cache = {}
        
        # Thread-safe queue for messages that need to be published to RabbitMQ
        self._message_queue = queue.Queue()
        
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
        if "mailing-lists/" in full_path and not filename.endswith(".json"):
            self._logger.debug(f"Skipping non-json mailing-list file: {relative_path}")
            return True
        if "newsgroups/" in full_path and not filename.endswith(".txt"):
            self._logger.debug(f"Skipping non-txt newsgroups file: {relative_path}")
            return True
        return False

    def _queue_for_publishing(self, relative_path):
        """Add file to thread-safe queue for later publishing to RabbitMQ."""
        self._logger.info(f"Queueing file for indexing: {relative_path}")
        msg = {"file_path": relative_path}
        self._message_queue.put(msg)
        
        # Update the cache with the current timestamp for this file
        now = datetime.now(timezone.utc)
        self._enqueued_cache[relative_path] = int(now.timestamp())

    def _check_and_queue_file(self, relative_path: str):
        """
        Revised to use an in-memory queue instead of directly publishing to RabbitMQ.
        """
        self._logger.debug(f"Checking file: {relative_path}")
        now = datetime.now(timezone.utc)
        
        # Check local cache for recent enqueue
        cache_timestamp = self._enqueued_cache.get(relative_path)
        if cache_timestamp and (int(now.timestamp()) - cache_timestamp) < self._REINDEXING_INTERVAL:
            self._logger.debug(f"File {relative_path} was recently queued at {cache_timestamp}. Skipping duplicate enqueue.")
            return
        
        if self._es_manager.record_exists(doc_id=relative_path):
            # Fetch the doc to query its status
            doc = self._es_manager.get_document(doc_id=relative_path) or {}
            
            if not self._SKIP_LLM_ENRICHMENT:
                # If the file is already indexed, verify it's been enriched:
                llm_summary = doc.get("_source", {}).get("llm_summary")
                if not llm_summary or llm_summary == OpenAIManager.AWAITING_LLM_ENRICHMENT:
                    self._logger.debug(f"File is indexed but not enriched: {relative_path}")
                    self._queue_for_publishing(relative_path)
                    return
                if self._ALWAYS_DO_LLM_ENRICHMENT:
                    self._logger.debug(f"Re-enriching file because ALWAYS_DO_LLM_ENRICHMENT is enabled: {relative_path}")
                    self._queue_for_publishing(relative_path)
                    return
                # Looks enriched and we aren't set to re-enrich
                self._logger.debug(f"File is already enriched: {relative_path}")
            if not self._REINDEXING_ENABLED:
                # Don't reindex if reindexing is disabled
                self._logger.debug(f"Already indexed and reindexing disabled, skipping file: {relative_path}")
                return
            else:
                last_indexed_str = doc.get("_source", {}).get("last_indexed")
                # Check if the file was indexed recently
                if last_indexed_str:
                    last_indexed = datetime.fromisoformat(last_indexed_str).replace(tzinfo=timezone.utc)
                    if last_indexed >= now - timedelta(seconds=self._REINDEXING_INTERVAL):
                        self._logger.debug(f"Reindexing enabled, but file was indexed recently: {relative_path}")
                        return
                self._logger.debug(f"Reindexing enabled, and file is due for reindexing: {relative_path}")
        
        self._queue_for_publishing(relative_path)
        
    def _process_file(self, relative_path):
        """Process an individual file for indexing in a separate thread."""
        try:
            self._logger.debug(f"Processing file (in thread): {relative_path}")
            self._check_and_queue_file(relative_path)
        except Exception as e:
            self._logger.error(f"Error processing file: {relative_path}: {e}")
            self._logger.exception(e)
            # Re-raise to be caught by the executor
            raise

    def walk_and_queue(self):
        """
        Walk through filesystem and queue files for processing in parallel using threads.
        Only the main thread publishes to RabbitMQ to ensure thread safety.
        """
        self._sparse_checkout_repo()
        
        # Set maximum number of pending futures to 3x the number of workers
        max_pending_futures = self._max_workers * 3
        
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            active_futures = []
            
            for root, dirs, files in os.walk(self._LOCAL_REPO_PATH):
                if ".git" in dirs:
                    dirs.remove(".git")
                
                self._logger.info(f"Processing files in {root}...")
                self._logger.debug(f"Number of files found: {len(files)}")
                
                # Publish any queued messages (from previous directories)
                self._publish_queued_messages()
                
                for filename in files:
                    full_path = os.path.join(root, filename).replace(os.path.sep, "/")
                    relative_path = os.path.relpath(full_path, self._LOCAL_REPO_PATH).replace(os.path.sep, "/")
                    
                    if self._should_skip_file(filename, full_path, relative_path):
                        continue
                    
                    # Check if we need to wait for some futures to complete
                    if len(active_futures) >= max_pending_futures:
                        # Wait for some futures to complete before continuing
                        self._process_completed_futures(active_futures)
                        # Publish any messages that were queued by completed futures
                        self._publish_queued_messages()
                    
                    self._logger.debug(f"Submitting file for processing: {relative_path}")
                    future = executor.submit(self._process_file, relative_path)
                    active_futures.append(future)
                
            # Process any remaining futures
            while active_futures:
                self._process_completed_futures(active_futures)
                # Publish any messages that were queued by completed futures
                self._publish_queued_messages()
            
            # Final check for any remaining messages
            self._publish_queued_messages(drain_all=True)
    
    def _publish_queued_messages(self, drain_all=False, batch_size=100):
        """
        Publish queued messages to RabbitMQ from the main thread.
        
        Args:
            drain_all: If True, drain the entire queue. Otherwise, process up to batch_size.
            batch_size: Maximum number of messages to process in one call.
        """
        published_count = 0
        
        # Process either up to batch_size or all messages if drain_all is True
        while not self._message_queue.empty() and (drain_all or published_count < batch_size):
            try:
                msg = self._message_queue.get_nowait()
                self._rabbitmq_manager.publish_message(item=msg)
                published_count += 1
                self._message_queue.task_done()
            except queue.Empty:
                break
            except Exception as e:
                self._logger.error(f"Error publishing message to RabbitMQ: {e}")
                self._logger.exception(e)
                
        if published_count > 0:
            self._logger.debug(f"Published {published_count} messages to RabbitMQ")
    
    def _process_completed_futures(self, active_futures):
        """Process completed futures and remove them from the active list."""
        # Wait for the first future to complete (timeout=0 means it returns immediately if nothing is done)
        done, _ = concurrent.futures.wait(
            active_futures, 
            timeout=None,  # Block until at least one future completes
            return_when=concurrent.futures.FIRST_COMPLETED
        )
        
        # Process completed futures
        for future in done:
            try:
                future.result()  # This will raise any exception that occurred in the thread
            except Exception as e:
                self._logger.error(f"Error in thread: {e}")
                self._logger.exception(e)
            active_futures.remove(future)