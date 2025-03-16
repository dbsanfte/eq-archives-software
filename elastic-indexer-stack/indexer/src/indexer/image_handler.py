import os
import logging
import base64

from .archive_handler import ArchiveHandler
from .openai_manager import OpenAIManager
from .es_manager import ElasticsearchManager

class ImageHandler:
    def __init__(self, archive_handler: ArchiveHandler, openai_manager: OpenAIManager, logger: logging.Logger=None, 
                 llm_enrichment_enabled: bool=True):
        self._archive_handler=archive_handler
        self._openai_manager=openai_manager
        self._logger=logger or logging.getLogger(__name__)
        self._LLM_ENRICHMENT_ENABLED=llm_enrichment_enabled
        if os.environ.get("SKIP_LLM_ENRICHMENT", "false") == "true":
            self._LLM_ENRICHMENT_ENABLED=False
    
    def process_image_file(self, relative_path, full_path, mime_type, domain_name) -> list[dict]:
        try:
            with open(full_path, "rb") as f:
                image_bytes = f.read()
                image_b64 = base64.b64encode(image_bytes)
            url = self._archive_handler._convert_to_archive_url(relative_path=relative_path)
            if not url or url == "":
                raise ValueError("Archive URL not found for image-type file.")
            alternate_url = self._archive_handler._strip_index_html_from_url(url=url)
            
            llm_response = {}
            try:
                if self._LLM_ENRICHMENT_ENABLED:
                    # Enrich the doc
                    llm_response = self._openai_manager.call_openai_api_image(image_b64=image_b64, 
                                                                            mime_type=mime_type, 
                                                                            domain_name=domain_name)
                else:
                    self._logger.info("LLM enrichment is disabled. Won't enrich this image.")
            except Exception as e:
                # We failed to enrich the doc, log the error and continue
                self._logger.error(f"Error calling OpenAI API for image: {e}")
                self._logger.exception(e)
                self._logger.warning("Skipping LLM enrichment for this image.")
                
            llm_model_name = llm_response.get("llm_model_name", None)
            llm_summary = llm_response.get("llm_summary", None)
            llm_image_text = llm_response.get("llm_image_text", [])
            llm_content_flavour = llm_response.get("llm_content_flavour", None)
            llm_image_text_vector = llm_response.get("llm_image_text_vector", None)
            llm_tags = llm_response.get("llm_tags", [])
                
            # Build document for image file
            doc_id = relative_path.replace(os.sep, '/')
            doc = ElasticsearchManager.build_document(
                id=doc_id,
                title=os.path.basename(relative_path),
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
            self._logger.error(f"Error processing image file: {e}")
            self._logger.exception(e)