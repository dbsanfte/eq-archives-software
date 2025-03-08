import os
import json
import logging
import re
import yaml
import numpy as np
from openai import OpenAI
import requests
import importlib.resources
from langchain_openai.embeddings import OpenAIEmbeddings
from langchain_core.documents import Document
from langchain_experimental.text_splitter import SemanticChunker

logger = logging.getLogger(__name__)

class OpenAIManager:
    def __init__(self, base_url=None, api_key=None, text_model_name=None, image_model_name=None,
                 api_key_file="/run/secrets/openai_api_key", embedding_model_name=None, 
                 prompt_pkg='indexer.resources.prompts', schema_pkg='indexer.resources.openai-api-schemas', 
                 text_temperature=None, image_temperature=None, default_prompt=None):
        
        self._base_url = base_url or os.environ.get("OPENAI_ENDPOINT", "http://localhost:1234/v1")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "lm-studio")
        if os.path.exists(api_key_file):
            with open(api_key_file, "r") as f:
                self._api_key = f.read().strip()
        self._text_model_name = text_model_name or os.environ.get("OPENAI_TEXT_MODEL_NAME", "")
        self._image_model_name = image_model_name or os.environ.get("OPENAI_IMAGE_MODEL_NAME", "")
        self._embedding_model_name = embedding_model_name or os.environ.get("OPENAI_EMBEDDING_MODEL_NAME", "unknown")
        self._schema_pkg = schema_pkg
        self._text_temperature = text_temperature or os.environ.get("OPENAI_TEXT_TEMPERATURE", 0.015)
        self._image_temperature = image_temperature or os.environ.get("OPENAI_IMAGE_TEMPERATURE", 0.015)
        
        # Initialize OpenAI client
        self._client = OpenAI(base_url=self._base_url, api_key=self._api_key)
        self._openai_embeddings = OpenAIEmbeddings(openai_api_base=self._base_url, 
                                                  api_key=self._api_key,
                                                  model=self._embedding_model_name,
                                                  # DS: This is needed for embeddings API compatibliity with LMStudio etc:
                                                  check_embedding_ctx_length=False)
        # DS: note: difficulties with models crashing during embedding
        # seem to be due to context windows on models being very small.
        # Embedding models with big context windows don't have such a problem
        # e.g.: nomic with 8k, vs snowflake with 512. Snowflake crashes a lot.
        
        # Load prompts cache and default prompt
        self._prompt_pkg = prompt_pkg
        self._prompts_cache = {}
        self._default_prompt = default_prompt or {}
        self._build_prompts_cache()

    def _build_prompts_cache(self) -> None:
        try:
            prompts_dir = importlib.resources.files(self._prompt_pkg)
            for f in prompts_dir.iterdir():
                if (f.name.endswith(".yml") or f.name.endswith(".yaml")) and f.is_file():
                    with f.open("r", encoding="utf-8") as yml_file:
                        data = yaml.safe_load(yml_file)
                        if f.name == "default.yml" or f.name == "default.yaml":
                            self._default_prompt.update(data)
                        else:
                            domain_regex = data.get("domain_match_regex", "")
                            if domain_regex:
                                self._prompts_cache[domain_regex] = data
        except FileNotFoundError:
            raise FileNotFoundError(f"Prompts package not found: {self._prompt_pkg}")

    def _resolve_prompt_for_task(self, content_type: str, task_type: str, domain_name: str) -> str:
        """Resolve the appropriate prompt based on domain name and type."""
        if domain_name:
            for regex, data in self._prompts_cache.items():
                if re.search(regex, domain_name):
                    content_prompts: dict = data.get(f"{content_type}_prompts")
                    if content_prompts:
                        return content_prompts.get(task_type)
                    raise ValueError(f"No {content_type} prompts found in {domain_name} file for task_type {task_type}.")
        
        content_prompts: dict = self._default_prompt.get(f"{content_type}_prompts")
        if content_prompts:
            return content_prompts.get(task_type)
        raise ValueError(f"No {content_type} prompts found in default file for task_type {task_type}.")
        
    def get_openai_embedding_client(self) -> OpenAIEmbeddings:
        return self._openai_embeddings
    
    def _parse_json_response(self, response: dict) -> dict:
        """Parse the JSON content from the LLM response."""
        try:
            return json.loads(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"No valid JSON from completion: {e}")
            raise ValueError("Invalid JSON response")

    def _resolve_schema_for_task(self, content_type: str, task_type: str) -> dict:
        """Resolve the appropriate schema based on content_type and task_type."""
        try:
            with importlib.resources.open_text(self._schema_pkg, f"{content_type}/{task_type}_schema.json") as file:
                return json.load(file)
        except Exception as e:
            logger.error(f"Error resoving schema for content_type {content_type} and task_type {task_type}: {e}")
            raise
        
    def _resolve_payload_for_task(self, content_type: str, prompt: str, schema: dict, 
                                  content: str, mime_type: str=None) -> dict:
        """Resolve the appropriate payload based on content_type and task_type."""
        if content_type == "text" or content_type == "other":
            return {
                "model": self._text_model_name,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "text", "text": content}
                    ]
                }],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "text_response",
                        "strict": "true",
                        "schema": schema
                    }
                },
                "temperature": self._text_temperature
            }
        elif content_type == "image":
            return {
                "model": self._image_model_name,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{content}"}} 
                    ]
                }],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "text_response",
                        "strict": "true",
                        "schema": schema
                    }
                },
                "temperature": self._image_temperature
            }
        else:
            raise ValueError(f"Unknown content_type: {content_type}")
            
    def _prompt_llm_for_task(self, content_type: str, content: str, task_type: str, 
                             domain_name: str, mime_type: str=None) -> dict:
        """Prompt the LLM for a specific task."""
        parsed_content = {}
        
        if domain_name == "groups.google.com" and task_type == "classification":
            # If the domain is 'groups.google.com", this is a Newsgroup Post. Don't bother inferencing.
            parsed_content["llm_content_flavour"] = "Newsgroup Post"
        elif domain_name == "groups.yahoo.com" and task_type == "classification":
            # If the domain is 'groups.yahoo.com", this is a Mailing List Email. Don't bother inferencing.
            parsed_content["llm_content_flavour"] = "Mailing List Email"
        else:
            prompt = self._resolve_prompt_for_task(content_type=content_type, task_type=task_type, 
                                                domain_name=domain_name)
            schema = self._resolve_schema_for_task(content_type=content_type, task_type=task_type)
            payload = self._resolve_payload_for_task(content_type=content_type, content=content, 
                                                    prompt=prompt, schema=schema, mime_type=mime_type)
            
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            
            resp = requests.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=120
            )
            resp.raise_for_status()
            
            llm_result = resp.json()
            parsed_content = self._parse_json_response(llm_result)
                        
        summary_vector = self.embed_text(parsed_content.get("llm_summary", ""))
        combined_image_text = " ".join(parsed_content.get("llm_image_text", []))
        image_text_vector = self.embed_text(combined_image_text)
        
        final_response = {
            "llm_model_name": self._text_model_name,
            "llm_summary": parsed_content.get("llm_summary", None),
            "llm_content_flavour": parsed_content.get("llm_content_flavour", None),
            "llm_extracted_dates": parsed_content.get("llm_extracted_dates", None),
            "llm_guessed_date": parsed_content.get("llm_guessed_date", None),
            "llm_summary_vector": summary_vector,
            "llm_image_text": parsed_content.get("llm_image_text", None),
            "llm_image_text_vector": image_text_vector,
            "llm_tags": parsed_content.get("llm_tags", None)
        }
        
        return final_response
    
    def _merge_dicts(self, base: dict, updates: dict) -> dict:
        for key, value in updates.items():
            if value is not None:
                base[key] = value
        return base

    def _call_openai_api(self, content: str, content_type: str, domain_name: str, mime_type: str=None) -> dict:
        """Call OpenAI API for processing."""
        try:
            result = {}
            
            # Classification
            result = self._merge_dicts(result, self._prompt_llm_for_task(content_type=content_type, content=content, 
                                      task_type="classification", domain_name=domain_name))
            
            # Summary
            result = self._merge_dicts(result, self._prompt_llm_for_task(content_type=content_type, content=content,
                                      task_type="summary", domain_name=domain_name))
            
            # Tagging
            result = self._merge_dicts(result, self._prompt_llm_for_task(content_type=content_type, content=content,
                                      task_type="tagging", domain_name=domain_name))
            
            if content_type == "text":
                # Date extraction
                result = self._merge_dicts(result, self._prompt_llm_for_task(content_type=content_type, content=content,
                                          task_type="date", domain_name=domain_name))
            
            if content_type == "image":
                # Image text extraction
                result = self._merge_dicts(result, self._prompt_llm_for_task(content_type=content_type, content=content,
                                          task_type="text_extraction", domain_name=domain_name, mime_type=mime_type))
            
            # Return the responses
            return result
        except Exception as e:
            logger.error(f"Error calling OpenAI for text file: {e}")
            logger.exception(e)
            raise e
        
    def call_openai_api_text(self, text_content: str, domain_name: str) -> dict:
        """Call OpenAI API for text processing."""
        return self._call_openai_api(content=text_content, content_type="text", 
                                     domain_name=domain_name)

    def call_openai_api_image(self, image_b64: str, mime_type: str, domain_name: str) -> dict:
        """Call OpenAI API for image processing."""
        return self._call_openai_api(content=image_b64, content_type="image", 
                                     mime_type=mime_type, domain_name=domain_name)
    
    def embed_text(self, text: str) -> np.array:
        """Embed text from OpenAI."""
        try:
            if not text or text.isspace():
                logger.debug(f"Empty text, returning a None embedding for text: {text}")
                return None
            logger.debug(f"Fetching embedding for: {text}")
            embedding = self._openai_embeddings.embed_query(text)
            if embedding is None:
                logger.error(f"Empty embedding for: {text}")
                raise ValueError("Empty embedding")
            return np.array(embedding, dtype=np.float32)
        except Exception as e:
            logger.error(f"Error fetching embedding: {e}")
            logger.exception(e)
            raise
        
    def get_chunks_and_embeddings(self, document: Document) -> list[dict]:
        # Chunk text intelligently
        chunks: list[Document] = []
        embedder: OpenAIEmbeddings = self.get_openai_embedding_client()
        splitter = SemanticChunker(embedder)
        chunks = splitter.split_documents([document])
        logger.debug(f"Chunks: {chunks}")
        
        # Embed each chunk
        chunk_vectors: list[dict] = []
        for chunk in chunks:
            chunk_vectors.append(
                {
                    "text_chunk": chunk.page_content, 
                    "vector": self.embed_text(text=chunk.page_content)
                }
            )
        # Return a list of the chunk texts and their embeddings
        return chunk_vectors

    