import os
import logging
import base64

from .archive_handler import ArchiveHandler
from .openai_manager import OpenAIManager
from .es_manager import ElasticsearchManager

logger = logging.getLogger(__name__)

class ImageHandler:
    def __init__(self, archive_handler: ArchiveHandler, openai_manager: OpenAIManager):
        self._archive_handler=archive_handler
        self._openai_manager=openai_manager
    
    def process_image_file(self, file_path, full_path, mime_type, domain_name) -> list[dict]:
        try:
            with open(full_path, "rb") as f:
                image_bytes = f.read()
                image_b64 = base64.b64encode(image_bytes)
            url = self._archive_handler._convert_to_archive_url(file_path=file_path)
            if not url or url == "":
                raise ValueError("Archive URL not found for image-type file.")
            alternate_url = self._archive_handler._strip_index_html_from_url(url=url)
            llm_response = self._openai_manager.call_openai_api_image(image_b64=image_b64, 
                                                                      mime_type=mime_type, 
                                                                      domain_name=domain_name)
            
            llm_model_name = llm_response.get("llm_model_name", None)
            llm_summary = llm_response.get("llm_summary", None)
            llm_image_text = llm_response.get("llm_image_text", [])
            llm_content_flavour = llm_response.get("llm_content_flavour", None)
            llm_image_text_vector = llm_response.get("llm_image_text_vector", None)
            llm_tags = llm_response.get("llm_tags", [])
                
            # Build document for image file
            doc_id = file_path.replace(os.sep, '/')
            doc = ElasticsearchManager.build_document(
                id=doc_id,
                title=os.path.basename(file_path),
                thumbnail=self._archive_handler._resolve_thumbnail_url(url=url, file_type="image"),
                file_type="image",
                mime_type=mime_type,
                domain_name=domain_name,
                url=url,
                alternate_url=alternate_url,
                llm_model_name=llm_model_name,
                llm_summary=llm_summary,
                llm_image_text=llm_image_text,
                llm_content_flavour=llm_content_flavour,
                llm_tags=llm_tags,
                llm_image_text_vector=llm_image_text_vector,
            )
            return [doc]
        except Exception as e:
            logger.error(f"Error processing image file: {e}")
            logger.exception(e)