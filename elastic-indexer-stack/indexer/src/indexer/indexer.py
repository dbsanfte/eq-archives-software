# indexer.py
import os
import logging
import dateparser
import magic

from .es_manager import ElasticsearchManager
from .openai_manager import OpenAIManager
from .image_handler import ImageHandler
from .text_handler import TextHandler
from .other_handler import OtherHandler
from .archive_handler import ArchiveHandler

class Indexer:
    def __init__(self, es_manager: ElasticsearchManager, openai_manager: OpenAIManager, archive_handler: ArchiveHandler=None,
                 logger: logging.Logger=None, skip_text_files: bool=False, skip_image_files: bool=False, skip_other_files: bool=False):
        self._logger = logger or logging.getLogger(__name__)
        self._es_manager = es_manager
        self._openai_manager = openai_manager
        self._archive_handler = archive_handler or ArchiveHandler()
        self._mime = magic.Magic(mime=True)

        # Instantiate filetype handlers
        self._image_handler = ImageHandler(self._archive_handler, self._openai_manager)
        self._text_handler = TextHandler(self._archive_handler, self._openai_manager)
        self._other_handler = OtherHandler(self._archive_handler, self._openai_manager)
        
        # Local repo path
        self._LOCAL_REPO_PATH = os.environ.get("LOCAL_REPO_PATH", "/data/eq-archives")
        
        # Skip flags
        self._SKIP_TEXT_FILES = skip_text_files
        if os.environ.get("SKIP_TEXT_FILES", "false").lower() == "true":
            self._SKIP_TEXT_FILES = True
        self._SKIP_IMAGE_FILES = skip_image_files
        if os.environ.get("SKIP_IMAGE_FILES", "false").lower() == "true":
            self._SKIP_IMAGE_FILES = True
        self._SKIP_OTHER_FILES = skip_other_files
        if os.environ.get("SKIP_OTHER_FILES", "false").lower() == "true":
            self._SKIP_OTHER_FILES = True
        
    def process_file(self, message: dict) -> None:
        file_path = message.get("file_path")
        self._logger.info(f"Got a file off the queue: {file_path}")
        if not file_path:
            e = ValueError("No file_path in RabbitMQ message!")
            self._logger.exception(e)
            self._logger.error(f"Indexing failed for file: {file_path}")
            return
            
        full_path = os.path.join(self._LOCAL_REPO_PATH, file_path)
        if not os.path.isfile(full_path):
            e = ValueError(f"File not found on disk: {full_path}")
            self._logger.exception(e)
            self._logger.error(f"Indexing failed for file: {file_path}")
            return

        try:
            docs: list[dict] = self._get_docs(file_path, full_path)
                    
            if len(docs) == 0:
                self._logger.error(f"No documents generated for file: {file_path}, therefore the file will not be indexed.")
                return
            self._logger.info(f"Generated {len(docs)} documents for file: {file_path}")
            for doc in docs:
                self._es_manager.index_document(doc.get("id", file_path), doc)
        except Exception as e:
            self._logger.exception(e)
            self._logger.error(f"Indexing failed for file: {file_path}")
            return
                
    def _get_docs(self, file_path: str, full_path: str) -> list[dict]:
        docs = []
        mime_type, domain_name, capture_date = self._get_file_metadata(file_path=file_path, full_path=full_path)
        
        if mime_type.startswith("text") or "json" in mime_type or "html" in mime_type:
            if self._SKIP_TEXT_FILES:
                self._logger.info("Skipping text file due to SKIP_TEXT_FILES flag.")
                return []
            else:
                docs = self._text_handler.process_text_file(relative_path=file_path, full_path=full_path, mime_type=mime_type, domain_name=domain_name)
        elif mime_type.startswith("image"):
            if self._SKIP_IMAGE_FILES:
                self._logger.info("Skipping image file due to SKIP_IMAGE_FILES flag.")
            else:
                docs = self._image_handler.process_image_file(relative_path=file_path, full_path=full_path, mime_type=mime_type, domain_name=domain_name)
        else:
            if self._SKIP_OTHER_FILES:
                self._logger.info("Skipping other file due to SKIP_OTHER_FILES flag.")
            else:
                docs = self._other_handler.process_other_file(relative_path=file_path, mime_type=mime_type, domain_name=domain_name)
        
        for doc in docs:
            # Set capture_dates:
            doc["capture_date"] = capture_date
        
        return docs
                
    def _get_file_metadata(self, file_path: str, full_path: str) -> tuple[str, str, str]:
        mime_type = magic.from_file(full_path, mime=True)
        domain_name, capture_date = self._archive_handler._extract_domain_and_date(relative_path=file_path, 
                                                                                 full_path=full_path)
        if capture_date is not None:
            parsed = dateparser.parse(capture_date)
            self._logger.debug(f"Parsed date: {parsed}")
            if parsed is not None:
                capture_date = parsed.isoformat()
                self._logger.debug(f"Setting ISO format capture_date: {capture_date}")
            else:
                self._logger.warning(f"Could not parse date: {capture_date}. Setting to None.")
                capture_date = None

        return mime_type, domain_name, capture_date