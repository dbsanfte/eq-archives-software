from unittest.mock import patch, MagicMock
import pytest
import requests
from langchain_core.documents import Document
from indexer.openai_manager import OpenAIManager
import importlib
import yaml
import json

@pytest.fixture
def openai_client():
    """Fixture to provide an instance of OpenAIManager."""
    with patch('openai.OpenAI'):
        client = OpenAIManager(
            base_url="http://test.com",
            api_key="test-key"
        )
        yield client

def test_get_chunks_and_embeddings(openai_client):
    """Test get_chunks_and_embeddings returns expected chunk texts and vectors."""
    # Create a dummy document
    doc = Document(page_content="This is a test document.")

    # Patch SemanticChunker to return predetermined chunks
    with patch('indexer.openai_manager.SemanticChunker') as mock_chunker:
        # Prepare dummy chunk documents
        dummy_chunks = [
            Document(page_content="Chunk 1 text"),
            Document(page_content="Chunk 2 text")
        ]
        mock_instance = mock_chunker.return_value
        mock_instance.split_documents.return_value = dummy_chunks

        # Patch embed_text to return a deterministic vector (using text length for simplicity)
        def fake_embed(text):
            return [len(text)]  # dummy vector for testing

        openai_client.embed_text = fake_embed

        result = openai_client.get_chunks_and_embeddings(doc)
        expected = [
            {"text_chunk": "Chunk 1 text", "vector": [len("Chunk 1 text")]},
            {"text_chunk": "Chunk 2 text", "vector": [len("Chunk 2 text")]}
        ]
        assert result == expected

def test_parse_json_response(openai_client):
    """Test JSON parsing of responses."""
    # Valid JSON response
    valid_response = {"choices": [{"message": {"content": '{"key": "value"}'}}]}
    parsed = openai_client._parse_json_response(valid_response)
    assert parsed == {'key': 'value'}
    # Invalid JSON should raise ValueError
    invalid_response = {"choices": [{"message": {"content": "invalid json"}}]}
    with pytest.raises(ValueError):
        openai_client._parse_json_response(invalid_response)

def test_call_openai_api_text_failure(openai_client):
    """Test call_openai_api_text raises an exception when the POST request fails."""
    with patch('requests.post') as mock_post:
        mock_post.side_effect = requests.exceptions.HTTPError("Bad Request")
        with pytest.raises(requests.exceptions.HTTPError):
            openai_client.call_openai_api_text("Test content", "test.com")

def test_call_openai_api_image_failure(openai_client):
    """Test call_openai_api_image raises an exception when the POST request fails."""
    with patch('requests.post') as mock_post:
        error_response = MagicMock()
        error_response.raise_for_status.side_effect = requests.exceptions.HTTPError("Error")
        mock_post.return_value = error_response
        with pytest.raises(requests.exceptions.HTTPError):
            openai_client.call_openai_api_image("base64data", "image/jpeg", "test.com")

def test_parse_json_response(openai_client):
    """Test the JSON parsing of responses."""
    # Valid response
    valid_response = {"choices": [{"message": {"content": '{"key": "value"}'}}]}
    parsed = openai_client._parse_json_response(valid_response)
    assert parsed == {'key': 'value'}
    
    # Invalid response
    invalid_response = {"choices": [{"message": {"content": "invalid json"}}]}
    with pytest.raises(ValueError):
        openai_client._parse_json_response(invalid_response)

def test_build_prompts_cache(openai_client, tmp_path):
    # Mock package structure
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    sample_file = prompts_dir / "sample.yml"
    sample_file.write_text("domain_match_regex: 'test.com'\ntext_prompts:\n  classification: 'MockPrompt'")
    with patch.object(importlib.resources, "files", return_value=prompts_dir):
        openai_client._build_prompts_cache()
        assert "test.com" in openai_client._prompts_cache

def test_resolve_prompt_for_task_domain_match(openai_client):
    # Manually inject a test domain prompt
    openai_client._prompts_cache = {
        "test.com": {
            "text_prompts": {
                "classification": "Domain-specific classification prompt"
            }
        }
    }
    result = openai_client._resolve_prompt_for_task(
        content_type="text", task_type="classification", domain_name="test.com"
    )
    assert result == "Domain-specific classification prompt"

def test_resolve_prompt_for_task_no_domain_match_falls_back(openai_client):
    # Manually set a default prompt
    openai_client._default_prompt = {
        "text_prompts": {
            "classification": "Default classification prompt"
        }
    }
    openai_client._prompts_cache = {}
    result = openai_client._resolve_prompt_for_task(
        content_type="text", task_type="classification", domain_name="another.com"
    )
    assert result == "Default classification prompt"

def test_resolve_schema_for_task(openai_client):
    # Mock schema file
    with patch("importlib.resources.open_text") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = json.dumps({"type": "object"})
        schema = openai_client._resolve_schema_for_task("text", "classification")
        assert schema["type"] == "object"

def test_resolve_payload_for_task_text(openai_client):
    schema = {"type": "object"}
    payload = openai_client._resolve_payload_for_task(
        content_type="text",
        prompt="Test prompt",
        schema=schema,
        content="Test content"
    )
    assert payload["model"] == openai_client._text_model_name

def test_resolve_payload_for_task_image(openai_client):
    schema = {"type": "object"}
    payload = openai_client._resolve_payload_for_task(
        content_type="image",
        prompt="Image prompt",
        schema=schema,
        content="BASE64DATA",
        mime_type="image/png"
    )
    assert payload["model"] == openai_client._image_model_name
    assert "image_url" in payload["messages"][0]["content"][1]

@patch("requests.post")
def test_prompt_llm_for_task(mock_post, openai_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": '{"llm_summary": "summary"}'}}]
    }
    mock_post.return_value = mock_resp
    openai_client._default_prompt = {
        "text_prompts": {
            "classification": "Default classification prompt"
        }
    }
    
    # Patch embed_text to return a deterministic vector (using text length for simplicity)
    def fake_embed(text):
        return [len(text)]  # dummy vector for testing
    
    # Patch _openai_embeddings.embed_query to use fake_embed so that embed_query() is defined.
    openai_client._openai_embeddings = MagicMock(embed_query=fake_embed)
    
    # Mock schema
    with patch("importlib.resources.open_text") as mock_file:
        
        mock_file.return_value.__enter__.return_value.read.return_value = json.dumps({"type": "object"})
        result = openai_client._prompt_llm_for_task(
            content_type="text",
            content="Test content",
            task_type="classification",
            domain_name="notfound.com"
        )
    assert result["llm_summary"] == "summary"

@patch("requests.post")
def test_call_openai_api_full_workflow_text(mock_post, openai_client):
    """
    Test the _call_openai_api() method with text content, ensuring
    classification, summary, tagging, and date-extraction steps all run and
    that the final result preserves each step’s output in the resulting union.
    """
    # Mock responses for each of the four calls: classification, summary, tagging, date
    classification_resp = MagicMock()
    classification_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"llm_content_flavour": "classification flavour"}'
                }
            }
        ]
    }

    summary_resp = MagicMock()
    summary_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"llm_summary": "summary summary"}'
                }
            }
        ]
    }

    tagging_resp = MagicMock()
    tagging_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"llm_tags":["tagging-tag1", "tagging-tag2"]}'
                }
            }
        ]
    }

    date_resp = MagicMock()
    date_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"llm_guessed_date": "2002-05-24", "llm_extracted_dates": [{"date": "2002-05-24"}, {"date": "2003-02-01"}]}'
                }
            }
        ]
    }
    
    # Patch embed_text to return a deterministic vector (using text length for simplicity)
    def fake_embed(text):
        return [len(text)]  # dummy vector for testing
    
    # Patch _openai_embeddings.embed_query to use fake_embed so that embed_query() is defined.
    openai_client._openai_embeddings = MagicMock(embed_query=fake_embed)
    
    # Requests.post is called 4 times in sequence for text content
    mock_post.side_effect = [
        classification_resp,
        summary_resp,
        tagging_resp,
        date_resp
    ]

    # Invoke the method
    result = openai_client._call_openai_api(
        content="Some text content",
        content_type="text",
        domain_name="test.com"
    )

    # Check final union of responses:
    # The last step overwrote repeated keys like llm_summary,
    # so we verify that the final dictionary has each step's values
    # and that all unique keys remain in place.
    assert result["llm_model_name"] == openai_client._text_model_name
    assert result["llm_summary"] == "summary summary"
    assert result["llm_content_flavour"] == "classification flavour"
    assert result["llm_extracted_dates"] == [{"date": "2002-05-24"}, {"date": "2003-02-01"}]
    assert result["llm_guessed_date"] == "2002-05-24"
    assert result["llm_tags"] == ["tagging-tag1", "tagging-tag2"]

    
