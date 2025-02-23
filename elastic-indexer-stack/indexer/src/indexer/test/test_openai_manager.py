from unittest.mock import patch, MagicMock
import pytest
from indexer.openai_manager import OpenAIManager
import requests

# Mock responses for testing
MOCK_TEXT_RESPONSE = {
    "choices": [{
        "message": {
            "content": '{"llm_summary": "Test Summary", "llm_content_flavour": "test", "llm_model_name": "test", "llm_summary_vector": [0.1, 0.2], "llm_guessed_date": "2022-01-01"}'
        }
    }]
}

MOCK_IMAGE_RESPONSE = {
    "choices": [{
        "message": {
            "content": '{"llm_summary": "Image Test", "llm_image_text": ["text1", "text2"]}'
        }
    }]
}

@pytest.fixture
def openai_client():
    """Fixture to provide an instance of OpenAIManager."""
    with patch('openai.OpenAI'):
        client = OpenAIManager(
            base_url="http://test.com",
            api_key="test-key"
        )
        yield client

def test_resolve_prompt(openai_client):
    """Test prompt resolution with a domain."""
    # Mock the prompts_cache and default_prompt
    openai_client._prompts_cache = {"example.com": {"text_prompt": "test prompt"}}
    openai_client._default_prompt = {"text_prompt": "default text"}
    # Test with a matching domain
    result = openai_client._resolve_prompt("text", "example.com")
    assert result == "test prompt"
    # Test without matching domain; should return default
    result = openai_client._resolve_prompt("text", "unknown.com")
    assert result == "default text"

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
