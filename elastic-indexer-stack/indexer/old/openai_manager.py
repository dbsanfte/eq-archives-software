# new file
import os
import json
import logging
import re
import yaml
from openai import OpenAI
import requests

logger = logging.getLogger(__name__)
client = OpenAI(base_url=os.environ.get("OPENAI_ENDPOINT", "http://localhost:1234/v1"),
                api_key=os.environ.get("OPENAI_API_KEY", "lm-studio"))

PROMPTS_CACHE = {}
DEFAULT_PROMPT = {}

def fetch_embedding(text):
    try:
        model = os.environ.get("OPENAI_EMBEDDING_MODEL_NAME", "unknown")
        text = text.replace("\n", " ")
        return client.embeddings.create(input = [text], model=model).data[0].embedding
    except Exception as e:
        logger.error(f"Error fetching embedding: {e}")
        logger.exception(e)
        raise e

def call_openai_api_text(text_content, domain_name):
    with open('./openai-api-schemas/text_schema.json', 'r') as file:
        schema = json.load(file)
    model = os.environ.get("OPENAI_TEXT_MODEL_NAME", "")
    temperature = os.environ.get("OPENAI_TEXT_TEMPERATURE", 0.6)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": resolve_prompt("text", domain_name)
                    },
                    {
                        "type": "text",
                        "text": f"{text_content}"
                    }
                ]
            }
        ],
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
    if os.environ.get("OPENAI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ.get('OPENAI_API_KEY')}"

    try:
        logger.debug(f"Calling OpenAI text API with: {payload}")
        endpoint = os.environ.get("OPENAI_ENDPOINT", "http://openai-service:8000")
        resp = requests.post(
            f"{endpoint}/chat/completions",
            headers=headers,
            json=payload,
            timeout=300
        )
        resp.raise_for_status()
        llm_result = resp.json()

        # Safely parse the response content into a flat dict
        try:
            parsed_content = json.loads(llm_result["choices"][0]["message"]["content"])
        except Exception:
            logger.error(f"No valid JSON from text completion, got: {parsed_content}")
            logger.exception(e)

        # Fetch embeddings
        text_vector = fetch_embedding(text_content)
        summary_vector = fetch_embedding(parsed_content.get("llm_summary", ""))

        # Flatten final result
        final_response = {
            "llm_model_name": model,
            "llm_summary": parsed_content.get("llm_summary", ""),
            "llm_content_flavour": parsed_content.get("llm_content_flavour", None),
            "llm_guessed_date": parsed_content.get("llm_guessed_date", None),
            "text_vector": text_vector,
            "llm_summary_vector": summary_vector
        }

        logger.debug(f"Final text API response: {final_response}")
        return final_response
    
    except Exception as e:
        logger.error(f"Error calling OpenAI text API: {e}")
        logger.exception(e)
        raise e

def call_openai_api_image(image_b64, mime_type, domain_name):
    with open('./openai-api-schemas/image_schema.json', 'r') as file:
        schema = json.load(file)
        
    model = os.environ.get("OPENAI_IMAGE_MODEL_NAME", "")
    temperature = os.environ.get("OPENAI_IMAGE_TEMPERATURE", 0.6)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": resolve_prompt("image", domain_name)
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}
                    }
                ]
            }
        ],
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
    if os.environ.get("OPENAI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ.get('OPENAI_API_KEY')}"

    try:
        logger.debug(f"Calling OpenAI image API with: {payload}")
        endpoint = os.environ.get("OPENAI_ENDPOINT", "http://openai-service:8000")
        resp = requests.post(
            f"{endpoint}/chat/completions",
            headers=headers,
            json=payload,
            timeout=300
        )
        resp.raise_for_status()
        llm_result = resp.json()

        # Safely parse the response content into a flat dict
        try:
            parsed_content = json.loads(llm_result["choices"][0]["message"]["content"])
        except Exception:
            logger.warning("No valid JSON from image completion, returning defaults.")
            parsed_content = {"llm_summary": None, "llm_image_text": [], "llm_content_flavour": None}

        # Combine image text for embedding
        combined_text = " ".join(parsed_content.get("llm_image_text", []))
        image_text_vector = fetch_embedding(combined_text)

        # Flatten final result
        final_response = {
            "llm_model_name": model,
            "llm_summary": parsed_content.get("llm_summary", None),
            "llm_image_text": parsed_content.get("llm_image_text", []),
            "llm_content_flavour": parsed_content.get("llm_content_flavour", None),
            "llm_image_text_vector": image_text_vector
        }

        logger.debug(f"Final image API response: {final_response}")
        return final_response
    
    except Exception as e:
        logger.error(f"Error calling OpenAI image API: {e}")
        logger.exception(e)
        raise e

def resolve_prompt(prompt_type, domain_name):
    # Build the prompts cache on first run
    if not PROMPTS_CACHE or not DEFAULT_PROMPT:
        build_prompts_cache()

    # Find a prompt based on domain regex
    if domain_name is not None:
        for domain_regex, data in PROMPTS_CACHE.items():
            if re.search(domain_regex, domain_name):
                if prompt_type == "text":
                    return data.get("text_prompt", "")
                elif prompt_type == "image":
                    return data.get("image_prompt", "")
    
    # Otherwise, use default prompt if no regex matched
    if prompt_type == "text":
        return DEFAULT_PROMPT.get("text_prompt", "")
    elif prompt_type == "image":
        return DEFAULT_PROMPT.get("image_prompt", "")
    else:
        return ""

def build_prompts_cache():
    prompts_dir = os.path.join(os.path.dirname(__file__), "prompts")
    if not os.path.isdir(prompts_dir):
        raise FileNotFoundError(f"Prompts directory not found: {prompts_dir}")

    for f in os.listdir(prompts_dir):
        if f.endswith(".yml"):
            path = os.path.join(prompts_dir, f)
            with open(path, "r", encoding="utf-8") as yml_file:
                data = yaml.safe_load(yml_file)
                if f == "default.yml":
                    DEFAULT_PROMPT.update(data)
                else:
                    domain_regex = data.get("domain_match_regex", "")
                    if domain_regex:
                        PROMPTS_CACHE[domain_regex] = data
