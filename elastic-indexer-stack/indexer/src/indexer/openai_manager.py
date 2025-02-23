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

logger = logging.getLogger(__name__)

class OpenAIManager:
    def __init__(self, base_url=None, api_key=None, text_model_name=None, image_model_name=None,
                 api_key_file="/run/secrets/openai_api_key", embedding_model_name=None, 
                 prompt_pkg='indexer.resources.prompts', schema_pkg='indexer.resources.openai-api-schemas', 
                 default_prompt=None):
        
        self._base_url = base_url or os.environ.get("OPENAI_ENDPOINT", "http://localhost:1234/v1")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "lm-studio")
        if os.path.exists(api_key_file):
            with open(api_key_file, "r") as f:
                self._api_key = f.read().strip()
        self._text_model_name = text_model_name or os.environ.get("OPENAI_TEXT_MODEL_NAME", "")
        self._image_model_name = image_model_name or os.environ.get("OPENAI_IMAGE_MODEL_NAME", "")
        self._embedding_model_name = embedding_model_name or os.environ.get("OPENAI_EMBEDDING_MODEL_NAME", "unknown")
        self._schema_pkg = schema_pkg
        
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

    def _resolve_prompt(self, prompt_type: str, domain_name: str) -> str:
        """Resolve the appropriate prompt based on domain name and type."""
        if not domain_name:
            return self._default_prompt.get(f"{prompt_type}_prompt", "")
        
        for regex, data in self._prompts_cache.items():
            if re.search(regex, domain_name):
                return data.get(f"{prompt_type}_prompt", "")
        
        return self._default_prompt.get(f"{prompt_type}_prompt", "")

    def get_openai_embedding_client(self) -> OpenAIEmbeddings:
        return self._openai_embeddings
    
    def embed_text(self, text: str) -> np.array:
        """Embed text from OpenAI."""
        try:
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

    def call_openai_api_text(self, text_content: str, domain_name: str) -> dict:
        """Call OpenAI API for text processing."""
        try:
            with importlib.resources.open_text(self._schema_pkg, "text_schema.json") as file:
                schema = json.load(file)
            
            temperature = os.environ.get("OPENAI_TEXT_TEMPERATURE", 0.6)
            prompt = self._resolve_prompt("text", domain_name)
            
            payload = {
                "model": self._text_model_name,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "text", "text": text_content}
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
                "temperature": temperature
            }
            
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            
            resp = requests.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=300
            )
            resp.raise_for_status()
            
            llm_result = resp.json()
            parsed_content = self._parse_json_response(llm_result)            
            summary_vector = self.embed_text(parsed_content.get("llm_summary", ""))
            
            final_response = {
                "llm_model_name": self._text_model_name,
                "llm_summary": parsed_content.get("llm_summary", ""),
                "llm_content_flavour": parsed_content.get("llm_content_flavour", None),
                "llm_guessed_date": parsed_content.get("llm_guessed_date", None),
                "llm_summary_vector": summary_vector,
                "llm_tags": parsed_content.get("llm_tags", [])
            }
            
            return final_response
            
        except Exception as e:
            logger.error(f"Error calling OpenAI text API: {e}")
            raise

    def call_openai_api_image(self, image_b64: str, mime_type: str, domain_name: str) -> dict:
        """Call OpenAI API for image processing."""
        try:
            with importlib.resources.open_text(self._schema_pkg, "image_schema.json") as file:
                schema = json.load(file)
            
            temperature = os.environ.get("OPENAI_IMAGE_TEMPERATURE", 0.6)
            prompt = self._resolve_prompt("image", domain_name)
            
            payload = {
                "model": self._image_model_name,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}} 
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
                "temperature": temperature
            }
            
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            
            resp = requests.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=300
            )
            resp.raise_for_status()
            
            llm_result = resp.json()
            parsed_content = self._parse_json_response(llm_result)
            
            combined_text = " ".join(parsed_content.get("llm_image_text", []))
            image_text_vector = self.embed_text(combined_text)
            
            final_response = {
                "llm_model_name": self._image_model_name,
                "llm_summary": parsed_content.get("llm_summary", None),
                "llm_image_text": parsed_content.get("llm_image_text", []),
                "llm_content_flavour": parsed_content.get("llm_content_flavour", None),
                "llm_tags": parsed_content.get("llm_tags", []),
                "llm_image_text_vector": image_text_vector
            }
            
            return final_response
            
        except Exception as e:
            logger.error(f"Error calling OpenAI image API: {e}")
            raise

    def _parse_json_response(self, response: dict) -> dict:
        """Parse the JSON content from the LLM response."""
        try:
            return json.loads(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"No valid JSON from completion: {e}")
            raise ValueError("Invalid JSON response")
