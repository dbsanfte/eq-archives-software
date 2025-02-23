import os
import logging

from .archive_handler import ArchiveHandler
from .es_manager import ElasticsearchManager

logger = logging.getLogger(__name__)

class OtherHandler:
    def __init__(self, archive_handler: ArchiveHandler):
        self.archive_handler=archive_handler

    def process_other_file(self, file_path: str, mime_type: str=None, domain_name: str=None) -> list[dict]:
        try:
            title = os.path.basename(file_path)
            url = self.archive_handler._convert_to_archive_url(file_path=file_path)
            if url == "":
                raise ValueError("Archive URL not found for other-type file.")
            alternate_url = self.archive_handler._strip_index_html_from_url(url=url)
            thumbnail = self.archive_handler._resolve_thumbnail_url(url=url, file_type="other")
            doc_id = file_path.replace(os.sep, '/')
            doc = ElasticsearchManager.build_document(
                id=doc_id,
                title=title,
                thumbnail=thumbnail,
                file_type="other",
                mime_type=mime_type,
                domain_name=domain_name,
                url=url,
                alternate_url=alternate_url
            )
            return [doc]
        except Exception as e:
            logger.error(f"Error processing other file type: {e}")
            logger.exception(e)