import os
import logging
import datetime
import json
from bs4 import BeautifulSoup
from langchain_core.documents import Document
from markdownify import markdownify as md
import numpy as np
import textwrap as tw
import chardet

from .archive_handler import ArchiveHandler
from .openai_manager import OpenAIManager
from .es_manager import ElasticsearchManager

class TextHandler:
    def __init__(self, archive_handler: ArchiveHandler, openai_manager: OpenAIManager, logger: logging.Logger=None,
                 llm_enrichment_enabled: bool=True):
        self._logger = logger or logging.getLogger(__name__)
        self._archive_handler=archive_handler
        self._openai_manager=openai_manager
        self._LLM_ENRICHMENT_ENABLED=llm_enrichment_enabled
        if os.environ.get("SKIP_LLM_ENRICHMENT", "false") == "true":
            self._LLM_ENRICHMENT_ENABLED=False
        
    def process_text_file(self, relative_path: str, full_path: str, mime_type: str, 
                          domain_name: str) -> list[dict]:
        try:
            # Parse into LangChain document
            documents: list[Document] = self._get_documents_from_file(relative_path=relative_path, full_path=full_path)
            url = self._archive_handler._convert_to_archive_url(relative_path=relative_path)
            if url == "":
                raise ValueError("Archive URL not found for text-type file.")
            alternate_url = self._archive_handler._strip_index_html_from_url(url=url)
            
            # Build the es doc and chunk it
            title = "Untitled"
            docs = []
            for document in documents:
                self._logger.debug(f"Document: {document}")
                # Resolve the doc title
                title = self._resolve_title_from_file(full_path=full_path, relative_path=relative_path)
                
                # Chunk the doc and get embeddings
                chunks_and_embeddings: list[dict] = self._openai_manager.get_chunks_and_embeddings(
                    document=document
                )
                
                llm_response = {}
                try:
                    if self._LLM_ENRICHMENT_ENABLED:
                        # Enrich the doc
                        llm_response = self._openai_manager.call_openai_api_text(text_content=document.page_content,
                                                                                domain_name=domain_name)
                    else:
                        self._logger.info("LLM enrichment is disabled. Won't enrich this doc.")
                except Exception as e:
                    # We failed at enrichment. Still index it though.
                    self._logger.error(f"Error calling OpenAI API for text file {relative_path}: {e}")
                    self._logger.exception(e)
                    self._logger.warning("Skipping LLM enrichment for this document.")
                
                llm_model_name = llm_response.get("llm_model_name", None)
                llm_summary = llm_response.get("llm_summary", OpenAIManager.AWAITING_LLM_ENRICHMENT)
                llm_content_flavour = llm_response.get("llm_content_flavour", None)
                llm_guessed_date = llm_response.get("llm_guessed_date", None)
                llm_extracted_dates = llm_response.get("llm_extracted_dates", None)
                llm_summary_vector = llm_response.get("llm_summary_vector", None)
                llm_tags = llm_response.get("llm_tags", None)
                
                mailing_list_name = None
                if self._archive_handler._mailing_lists_path in relative_path:
                    # Also grab the mailing-list name for mailing lists
                    mailing_list_name = os.path.basename(os.path.dirname(relative_path))    
                
                # Build the doc
                doc = ElasticsearchManager.build_document(
                    id = relative_path.replace(os.sep, '/'),
                    title = title,
                    file_type = "text",
                    mime_type = mime_type,
                    text = chunks_and_embeddings,
                    text_full = document.page_content,
                    llm_summary = llm_summary,
                    llm_summary_vector = llm_summary_vector,
                    llm_guessed_date = llm_guessed_date,
                    llm_extracted_dates = llm_extracted_dates,
                    llm_model_name = llm_model_name,
                    llm_content_flavour = llm_content_flavour,
                    llm_tags = llm_tags,
                    domain_name = domain_name,
                    mailing_list_name = mailing_list_name,
                    url = url,
                    alternate_url = alternate_url,
                    thumbnail = self._archive_handler._resolve_thumbnail_url(url=url, file_type="text")
                )
                docs.append(doc)                
            return docs
        except Exception as e:
            self._logger.error(f"Error processing text file {relative_path}: {e}")
            self._logger.exception(e)
            raise e

    def _get_documents_from_file(self, relative_path: str, full_path: str) -> list[Document]:
        """
        Preprocess a file before loading it into LangChain.
        """
        preprocessed_content = None
    
        if (self._archive_handler._newsgroups_path in relative_path):
            preprocessed_content = self._preprocess_newsgroup_file(full_path=full_path)
        elif (self._archive_handler._mailing_lists_path in relative_path):
            preprocessed_content = self._preprocess_mailing_list_file(full_path=full_path)
        elif (self._archive_handler._websites_path in relative_path):
            preprocessed_content = self._preprocess_website_file(full_path=full_path, relative_path=relative_path)
        else:
            self._logger.warning(f"File {full_path} is not in a recognized archive path. Will treat as html.")
            with open(full_path, "r") as file:
                content = file.read()
                preprocessed_content = md(content)
        
        doc = Document(page_content=preprocessed_content, metadata={"source": full_path})
        return [doc]
        
    def _preprocess_newsgroup_file(self, full_path: str) -> str:
        with open(full_path, "r") as file:
            content = file.read()
                        
            # Split into sections
            sections = content.split("\n\n")
            header_section = sections[0]
            # Concatenate all sections from index 1 onward, this is the complete text:
            text_section = "\n\n".join(sections[1:])  
                        
            # We only keep a few headers for our purposes:
            headers = header_section.split("\n")
            from_line = [line for line in headers if line.startswith("From: ")][0]
            newsgroup_line = [line for line in headers if line.startswith("Newsgroups: ")][0]
            subject_line = [line for line in headers if line.startswith("Subject: ")][0]
            date_line = [line for line in headers if line.startswith("Date: ")][0]
                        
            # Convert any HTML to Markdown while stripping out script and style tags
            text = f"""
                <title><h1>{subject_line}</h1></title><br/>
                <h3>{from_line}</h3>
                <h3>{newsgroup_line}</h3>
                <h3>{date_line}</h3>
                <hr/>
                {text_section}
            """
            md_content = md(tw.dedent(text).strip())
            return md_content
        
    def _preprocess_mailing_list_file(self, full_path: str) -> str:
        if not full_path.endswith(".json"):
            raise ValueError("Mailing list file must be a JSON file.")
        
        capture_date = self._archive_handler.get_mailing_list_date(full_path=full_path)
        
        with open(full_path, "r") as file:
            content = file.read()
            json_file = json.loads(content)
            
            text_section = json_file["ygData"]["messageBody"]
            
            subject_line = "Subject: "
            try:
                subject_line = "Subject: " + json_file["ygData"]["subject"]
            except KeyError:
                self._logger.warning(f"Subject not found in JSON file {full_path}. Using default subject.")
            
            group_line = "Mailing-list: " + os.path.basename(os.path.dirname(full_path))
            
            if capture_date is not None:
                date_line = "Date: " + capture_date
            else:
                date_line = "Date: Unknown"
                
            from_line = "From: " + json_file["ygData"]["from"]
            
            text = f"""
                <title><h1>{subject_line}</h1></title><br/>
                <h3>{from_line}</h3>
                <h3>{group_line}</h3>
                <h3>{date_line}</h3>
                <hr/>
                {text_section}
            """
            md_content = md(tw.dedent(text).strip())
            return md_content
        
    def _preprocess_website_file(self, full_path: str, relative_path: str) -> str:
        file_content, _ = self._detect_and_read_file(full_path)
        
        if file_content is None:
            raise ValueError(f"Could not decode file {full_path} with any encoding.")
        
        url = self._archive_handler._convert_to_archive_url(relative_path=relative_path)
        content = f"<b>Page URL:</b> {url}<br/><hr/>{file_content}"
        md_content = md(content)
        return md_content

    def _resolve_title_from_file(self, full_path: str, relative_path: str) -> str:
        """
        Extract/Resolve an appropriate title from a text-type file.
        """
        title = "Untitled"
        
        if self._archive_handler._websites_path in relative_path:
            file_content, _ = self._detect_and_read_file(full_path)
            
            if file_content is None:
                self._logger.warning(f"Could not decode file {full_path} with any encoding. Using default title.")
                return title
                
            soup = BeautifulSoup(file_content, 'html.parser')
            title_tag = soup.find('title')
            if title_tag:
                title = title_tag.string
                self._logger.debug(f"Title from HTML: {title}")
            else:
                self._logger.debug("Couldn't find title tag in HTML file, falling back to default.")
        elif self._archive_handler._newsgroups_path in relative_path:
            with open(full_path, 'r') as file:
                for line in file:
                    if line.startswith('Subject: '):
                        title = line[9:].strip()
                        self._logger.debug(f"Title from Newsgroup: {title}")
                        break
                if title == "Untitled":
                    self._logger.warning(
                        f"Couldn't find subject line in Newsgroup file {full_path}, falling back to default.")
        elif self._archive_handler._mailing_lists_path in relative_path:
            if not full_path.endswith(".json"):
                raise ValueError("Mailing list file must be a JSON file.")
            with open(full_path, 'r') as file:
                json_file = json.loads(file.read())
                title = "Untitled"
                try:
                    title = json_file["ygData"]["subject"]
                except KeyError:
                    self._logger.warning(f"Subject not found in JSON file {full_path}, using default title.")
                self._logger.debug(f"Title from Mailing List: {title}")
        else:
            title = os.path.basename(full_path)
            self._logger.debug(f"Title from Filename: {title}")
        
        return title

    def _detect_and_read_file(self, file_path: str) -> tuple[str, str]:
        """
        Detect file encoding and read the file content.
        Returns a tuple of (content, encoding used) or (None, None) if file can't be read.
        """
        file_content = None
        encoding_used = None
        
        try:
            # Read the file in binary mode first
            with open(file_path, 'rb') as binary_file:
                raw_data = binary_file.read(10000)  # Read first chunk to detect encoding
                detection_result = chardet.detect(raw_data)
            
            encoding = detection_result['encoding']
            confidence = detection_result['confidence']
            self._logger.debug(f"Detected encoding: {encoding} with confidence: {confidence}")
            
            # Try with detected encoding
            try:
                with open(file_path, 'r', encoding=encoding) as file:
                    file_content = file.read()
                    encoding_used = encoding
            except UnicodeDecodeError:
                # If detected encoding fails, try our fallbacks
                self._logger.warning(f"Detected encoding {encoding} failed for {file_path}")
                for fallback_encoding in ['utf-8', 'cp1252', 'latin-1']:
                    if fallback_encoding != encoding:  # Don't try the same encoding twice
                        try:
                            with open(file_path, 'r', encoding=fallback_encoding) as file:
                                file_content = file.read()
                                encoding_used = fallback_encoding
                                self._logger.debug(f"Successfully read with fallback encoding: {fallback_encoding}")
                                break
                        except UnicodeDecodeError:
                            continue
            if file_content is None:
                raise ValueError(f"Failed to read file {file_path} with all attempted encodings.")
            
        except Exception as e:
            self._logger.error(f"Error during encoding detection for {file_path}: {e}")
            self._logger.exception(e)
        
        return file_content, encoding_used