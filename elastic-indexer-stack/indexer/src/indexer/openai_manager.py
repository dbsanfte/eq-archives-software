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

class OpenAIManager:
    AWAITING_LLM_ENRICHMENT = "[ Still awaiting LLM Enrichment... ]"
    
    def __init__(self, base_url: str | None = None, 
                 base_url_completions: str | None = None, 
                 base_url_embeddings: str | None = None,
                 api_key: str | None = None, 
                 text_model_name: str | None = None, 
                 image_model_name: str | None = None,
                 api_key_file: str = "/run/secrets/openai_api_key", 
                 embedding_model_name: str | None = None, 
                 prompt_pkg: str = 'indexer.resources.prompts', 
                 schema_pkg: str = 'indexer.resources.openai-api-schemas', 
                 text_temperature: float | None = None, 
                 image_temperature: float | None = None, 
                 max_completion_tokens: int | None = None, 
                 reasoning_effort: str | None = None, 
                 default_prompt: dict | None = None, 
                 default_timeout: int | None = None, 
                 logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger(__name__)
        
        self._base_url = base_url or os.environ.get("OPENAI_ENDPOINT", None)
        self._base_url_completions = base_url_completions or os.environ.get("OPENAI_ENDPOINT_COMPLETIONS", None)
        self._base_url_embeddings = base_url_embeddings or os.environ.get("OPENAI_ENDPOINT_EMBEDDINGS", None)
        
        if not self._base_url_completions:
            self._base_url_completions = self._base_url
            if not self._base_url_completions:
                raise ValueError("Base URL for completions is not set. Please provide a valid URL.")
            self._logger.debug(f"Using base URL for completions: {self._base_url_completions}")
        if not self._base_url_embeddings:
            self._base_url_embeddings = self._base_url
            if not self._base_url_embeddings:
                raise ValueError("Base URL for embeddings is not set. Please provide a valid URL.")
            self._logger.debug(f"Using base URL for embeddings: {self._base_url_embeddings}")
            
        # Priority order: constructor param -> env var -> secret file -> None
        self._api_key = api_key
        if self._api_key is None:
            self._api_key = os.environ.get("OPENAI_API_KEY", None)
        if self._api_key is None and os.path.exists(api_key_file):
            with open(api_key_file, "r") as f:
                file_content = f.read().strip()
                if file_content:
                    self._api_key = file_content
                    
        self._text_model_name = text_model_name or os.environ.get("OPENAI_TEXT_MODEL_NAME", "")
        self._image_model_name = image_model_name or os.environ.get("OPENAI_IMAGE_MODEL_NAME", "")
        self._embedding_model_name = embedding_model_name or os.environ.get("OPENAI_EMBEDDING_MODEL_NAME", "unknown")
        self._schema_pkg = schema_pkg
        self._text_temperature = text_temperature if text_temperature is not None else float(os.environ.get("OPENAI_TEXT_TEMPERATURE", "0.015"))
        if self._text_temperature < 0.0:
            self._logger.warning(f"Text temperature is negative: {self._text_temperature}. Setting to None.")
            self._text_temperature = None
        self._image_temperature = image_temperature if image_temperature is not None else float(os.environ.get("OPENAI_IMAGE_TEMPERATURE", "0.015"))
        if self._image_temperature < 0.0:
            self._logger.warning(f"Image temperature is negative: {self._image_temperature}. Setting to None.")
            self._image_temperature = None
        self._reasoning_effort = reasoning_effort or os.environ.get("OPENAI_REASONING_EFFORT", None)
        self._default_timeout = default_timeout if default_timeout is not None else int(os.environ.get("OPENAI_DEFAULT_TIMEOUT", "300"))
        self._max_completion_tokens = max_completion_tokens if max_completion_tokens is not None else int(os.environ.get("OPENAI_MAX_COMPLETION_TOKENS", "4096"))
        if self._max_completion_tokens < 0:
            self._logger.warning(f"Max completion tokens is negative: {self._max_completion_tokens}. Setting to None.")
            self._max_completion_tokens = None
        
        # Initialize OpenAI client
        self._client = OpenAI(base_url=self._base_url_completions, api_key=self._api_key)
        self._openai_embeddings = OpenAIEmbeddings(base_url=self._base_url_embeddings, 
                                                  api_key=self._api_key,
                                                  model=self._embedding_model_name,
                                                  # DS: This is needed for embeddings API compatibliity with LMStudio etc:
                                                  check_embedding_ctx_length=False,
                                                  timeout=self._default_timeout)
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
        # Check if there is a domain-name specific prompt
        self._logger.debug(f"Resolving prompt for content_type: {content_type}, task_type: {task_type}, domain_name: {domain_name}")
        if domain_name:
            for regex, data in self._prompts_cache.items():
                if re.search(regex, domain_name):
                    content_prompts: dict = data.get(f"{content_type}_prompts")
                    if content_prompts:
                        prompt = content_prompts.get(task_type)
                        assert prompt is not None, f"No prompt found for task_type {task_type} in {domain_name} prompts."
                        return prompt
                    raise ValueError(f"No {content_type} prompts found in {domain_name} file for task_type {task_type}.")
        
        # If no domain match, use default prompts
        self._logger.debug(f"No domain-specific prompt found, using default prompts for content_type: {content_type}, task_type: {task_type}")
        content_prompts: dict = self._default_prompt.get(f"{content_type}_prompts", {})
        if len(content_prompts) == 0:
            self._logger.warning(f"No prompts found for content_type {content_type} in default prompts.")
            raise ValueError(f"No prompts found for content_type {content_type} in default prompts.")
        
        if content_prompts:
            prompt = content_prompts.get(task_type)
            assert prompt is not None, f"No prompt found for task_type {task_type} in default prompts."
            return prompt
        raise ValueError(f"No {content_type} prompts found in default file for task_type {task_type}.")
        
    def get_openai_embedding_client(self) -> OpenAIEmbeddings:
        return self._openai_embeddings
    
    def _parse_json_response(self, response: dict) -> dict:
        """Parse the JSON content from the LLM response."""
        try:
            return json.loads(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            self._logger.error(f"No valid JSON from completion: {e}")
            raise ValueError("Invalid JSON response")

    def _resolve_schema_for_task(self, content_type: str, task_type: str) -> dict:
        """Resolve the appropriate schema based on content_type and task_type."""
        try:
            with importlib.resources.open_text(self._schema_pkg, f"{content_type}/{task_type}_schema.json") as file:
                return json.load(file)
        except Exception as e:
            self._logger.error(f"Error resoving schema for content_type {content_type} and task_type {task_type}: {e}")
            raise
        
    def _resolve_payload_for_task(self, content_type: str, prompt: str, schema: dict, 
                                  content: str, mime_type: str | None = None) -> dict:
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
                "temperature": self._text_temperature,
                "max_completion_tokens": self._max_completion_tokens,
                "reasoning_effort": self._reasoning_effort
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
                "temperature": self._image_temperature,
                "max_completion_tokens": self._max_completion_tokens,
                "reasoning_effort": self._reasoning_effort
            }
        else:
            raise ValueError(f"Unknown content_type: {content_type}")
            
    def _prompt_llm_for_task(self, content_type: str, content: str, task_type: str, 
                             domain_name: str, mime_type: str | None = None) -> dict:
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
                f"{self._base_url_completions}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self._default_timeout
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

    def _call_openai_api(self, content: str, content_type: str, domain_name: str, mime_type: str | None = None) -> dict:
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
            self._logger.error(f"Error calling OpenAI for text file: {e}")
            self._logger.exception(e)
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
                self._logger.debug(f"Empty text, returning a None embedding for text: {text}")
                return None
            self._logger.debug(f"Fetching embedding for: {text}")
            embedding = self._openai_embeddings.embed_query(text)
            if embedding is None:
                self._logger.error(f"Empty embedding for: {text}")
                raise ValueError("Empty embedding")
            return np.array(embedding, dtype=np.float32)
        except Exception as e:
            self._logger.error(f"Error fetching embedding: {e}")
            self._logger.exception(e)
            raise
        
    def get_chunks_and_embeddings(self, document: Document) -> list[dict]:
        # Chunk text intelligently
        chunks: list[Document] = []
        embedder: OpenAIEmbeddings = self.get_openai_embedding_client()
        splitter = SemanticChunker(embedder)
        chunks = splitter.split_documents([document])
        self._logger.debug(f"Chunks: {chunks}")
        
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

    