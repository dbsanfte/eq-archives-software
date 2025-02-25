import os
import logging
from docling.document_converter import DocumentConverter

from .archive_handler import ArchiveHandler
from .openai_manager import OpenAIManager
from .es_manager import ElasticsearchManager

logger = logging.getLogger(__name__)

class OtherHandler:
    def __init__(self, archive_handler: ArchiveHandler, openai_manager: OpenAIManager):
        self._openai_manager=openai_manager
        self._archive_handler=archive_handler

    def process_other_file(self, file_path: str, mime_type: str=None, domain_name: str=None) -> list[dict]:
        try:
            title = os.path.basename(file_path)
            url = self._archive_handler._convert_to_archive_url(file_path=file_path)
            if url == "":
                raise ValueError("Archive URL not found for other-type file.")
            alternate_url = self._archive_handler._strip_index_html_from_url(url=url)
            thumbnail = self._archive_handler._resolve_thumbnail_url(url=url, file_type="other")
            doc_id = file_path.replace(os.sep, '/')
            markdown = self._extract_text_from_other_file(file_path=file_path)
            
            chunks_and_embeddings = self._openai_manager.get_chunks_and_embeddings(
                text=markdown
            )
            
            # Enrich the doc
            llm_response = self._openai_manager.call_openai_api_text(text_content=markdown,
                                                                        domain_name=domain_name)
            llm_model_name = llm_response.get("llm_model_name", None)
            llm_summary = llm_response.get("llm_summary", "")
            llm_content_flavour = llm_response.get("llm_content_flavour", None)
            llm_guessed_date = llm_response.get("llm_guessed_date", None)
            llm_extracted_dates = llm_response.get("llm_extracted_dates", None)
            llm_summary_vector = llm_response.get("llm_summary_vector", None)
            llm_tags = llm_response.get("llm_tags", [])
            
            # Build the doc
            doc = ElasticsearchManager.build_document(
                id=doc_id,
                title=title,
                thumbnail=thumbnail,
                file_type="other",
                mime_type=mime_type,
                text_full=markdown,
                llm_summary = llm_summary,
                llm_summary_vector = llm_summary_vector,
                llm_guessed_date = llm_guessed_date,
                llm_extracted_dates = llm_extracted_dates,
                llm_model_name = llm_model_name,
                llm_content_flavour = llm_content_flavour,
                llm_tags = llm_tags,
                domain_name=domain_name,
                url=url,
                text=chunks_and_embeddings,
                alternate_url=alternate_url
            )
            return [doc]
        except Exception as e:
            logger.error(f"Error processing other file type: {e}")
            logger.exception(e)
            
    def _preprocess_other_file(self, file_path: str) -> str:
        try:
            converter = DocumentConverter()
            result = converter.convert(file_path)
            return result.document.export_to_markdown()
        except Exception as e:
            logger.error(f"Error preprocessing other file type: {e}")
            logger.exception(e)
            raise e
        
    def _extract_text_from_other_file(self, file_path: str) -> str:
        try:
            with open(file_path, "r") as file:
                return file.read()
        except Exception as e:
            logger.error(f"Error extracting text from other file type: {e}")
            logger.exception(e)
            raise e
        