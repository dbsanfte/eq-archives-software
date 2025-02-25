import os
import logging
import datetime
import json
from bs4 import BeautifulSoup
from langchain_core.documents import Document
from markdownify import markdownify as md
import numpy as np
import textwrap as tw

from .archive_handler import ArchiveHandler
from .openai_manager import OpenAIManager
from .es_manager import ElasticsearchManager

logger = logging.getLogger(__name__)

class TextHandler:
    def __init__(self, archive_handler: ArchiveHandler, openai_manager: OpenAIManager):
        self._archive_handler=archive_handler
        self._openai_manager=openai_manager
        
    def process_text_file(self, file_path: str, full_path: str, mime_type: str, 
                          domain_name: str) -> list[dict]:
        try:
            # Parse into LangChain document
            documents: list[Document] = self._get_documents_from_file(relative_path=file_path, full_path=full_path)
            url = self._archive_handler._convert_to_archive_url(file_path=file_path)
            if url == "":
                raise ValueError("Archive URL not found for text-type file.")
            alternate_url = self._archive_handler._strip_index_html_from_url(url=url)
            
            # Build the es doc and chunk it
            title = "Untitled"
            docs = []
            for document in documents:
                logger.debug(f"Document: {document}")
                # Resolve the doc title
                title = self._resolve_title_from_file(full_path=full_path)
                
                # Chunk the doc and get embeddings
                chunks_and_embeddings: list[dict] = self._openai_manager.get_chunks_and_embeddings(
                    document=document
                )
                
                # Enrich the doc
                llm_response = self._openai_manager.call_openai_api_text(text_content=document.page_content,
                                                                         domain_name=domain_name)
                llm_model_name = llm_response.get("llm_model_name", None)
                llm_summary = llm_response.get("llm_summary", "")
                llm_content_flavour = llm_response.get("llm_content_flavour", None)
                llm_guessed_date = llm_response.get("llm_guessed_date", None)
                llm_extracted_dates = llm_response.get("llm_extracted_dates", None)
                llm_summary_vector = llm_response.get("llm_summary_vector", None)
                llm_tags = llm_response.get("llm_tags", [])
                
                mailing_list_name = None
                if self._archive_handler.mailing_lists_path in file_path:
                    # Also grab the mailing-list name for mailing lists
                    mailing_list_name = os.path.basename(os.path.dirname(file_path))    
                
                # Build the doc
                doc = ElasticsearchManager.build_document(
                    id = file_path.replace(os.sep, '/'),
                    title = title,
                    file_type = "text",
                    mime_type = mime_type,
                    text = chunks_and_embeddings,
                    text_full = md(document.page_content, strip=["script", "style"]),
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
            logger.error(f"Error processing text file {file_path}: {e}")
            logger.exception(e)
            raise e

    def _get_documents_from_file(self, relative_path: str, full_path: str) -> list[Document]:
        """
        Preprocess a file before loading it into LangChain.
        """
        preprocessed_content = None
    
        if (self._archive_handler.newsgroups_path in relative_path):
            preprocessed_content = self._preprocess_newsgroup_file(full_path=full_path)
        elif (self._archive_handler.mailing_lists_path in relative_path):
            preprocessed_content = self._preprocess_mailing_list_file(full_path=full_path)
        elif (self._archive_handler.websites_path in relative_path):
            preprocessed_content = self._preprocess_website_file(full_path=full_path)
        else:
            logger.warning(f"File {full_path} is not in a recognized archive path. Will treat as html.")
            with open(full_path, "r") as file:
                content = file.read()
                preprocessed_content = md(content, strip=["script", "style"])
        
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
            md_content = md(tw.dedent(text).strip(), strip=["script", "style"])
            return md_content
        
    def _preprocess_mailing_list_file(self, full_path: str) -> str:
        if not full_path.endswith(".json"):
            raise ValueError("Mailing list file must be a JSON file.")
        with open(full_path, "r") as file:
            content = file.read()
            json_file = json.loads(content)
            
            text_section = json_file["ygData"]["messageBody"]
            
            subject_line = "Subject: " + json_file["ygData"]["subject"]
            group_line = "Mailing-list: " + os.path.basename(os.path.dirname(full_path))
            date_line = "Date: " + datetime.datetime.fromtimestamp(json_file["ygData"]["date"], 
                                                                 datetime.timezone.utc).isoformat()
            from_line = "From: " + json_file["ygData"]["from"]
            
            text = f"""
                <title><h1>{subject_line}</h1></title><br/>
                <h3>{from_line}</h3>
                <h3>{group_line}</h3>
                <h3>{date_line}</h3>
                <hr/>
                {text_section}
            """
            md_content = md(tw.dedent(text).strip(), strip=["script", "style"])
            return md_content
        
    def _preprocess_website_file(self, full_path: str) -> str:
        with open(full_path, "r") as file:
            file_content = file.read()
            url = self._archive_handler._convert_to_archive_url(file_path=full_path)
            content = f"<b>Page URL:</b> {url}<br/><hr/>{file_content}"
            md_content = md(content, strip=["script", "style"])
            return md_content

    def _resolve_title_from_file(self, full_path: str) -> str:
        """
        Extract/Resolve an appropriate title from a text-type file.
        """
        title = "Untitled"
        
        if self._archive_handler.websites_path in full_path:
            with open(full_path, 'r') as file:
                soup = BeautifulSoup(file, 'html.parser')
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.string
                    logger.debug(f"Title from HTML: {title}")
                else:
                    logger.debug("Couldn't find title tag in HTML file, falling back to default.")
        elif self._archive_handler.newsgroups_path in full_path:
            with open(full_path, 'r') as file:
                for line in file:
                    if line.startswith('Subject: '):
                        title = line[9:].strip()
                        logger.debug(f"Title from Newsgroup: {title}")
                        break
                if title == "Untitled":
                    logger.warning(
                        f"Couldn't find subject line in Newsgroup file {full_path}, falling back to default.")
        elif self._archive_handler.mailing_lists_path in full_path:
            if not full_path.endswith(".json"):
                raise ValueError("Mailing list file must be a JSON file.")
            with open(full_path, 'r') as file:
                json_file = json.loads(file.read())
                title = json_file["ygData"]["subject"] or "Untitled"
                logger.debug(f"Title from Mailing List: {title}")
        else:
            title = os.path.basename(full_path)
            logger.debug(f"Title from Filename: {title}")
        
        return title