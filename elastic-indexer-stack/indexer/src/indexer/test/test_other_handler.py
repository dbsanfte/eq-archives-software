import os
import pytest

from indexer.other_handler import OtherHandler
from indexer.es_manager import ElasticsearchManager
from unittest.mock import MagicMock, patch, mock_open
from docling.document_converter import DocumentConverter

# Dummy implementations
class DummyArchiveHandler:
    def _convert_to_archive_url(self, relative_path: str) -> str:
        return "http://dummy.archive/" + relative_path.replace(os.sep, "/")
    def _strip_index_html_from_url(self, url: str) -> str:
        return url.replace("index.html", "")
    def _resolve_thumbnail_url(self, url: str, file_type: str) -> str:
        return "http://dummy.thumbnail/other"

class DummyArchiveHandlerEmptyURL(DummyArchiveHandler):
    def _convert_to_archive_url(self, file_path: str) -> str:
        return ""

class DummyOpenAIManager:
    def get_chunks_and_embeddings(self, text):
        return [{"text_chunk": text, "vector": [1, 2, 3]}]
    def call_openai_api_text(self, text_content: str, domain_name: str) -> dict:
        return {
            "llm_summary": "dummy summary",
            "llm_summary_vector": [0.1, 0.2],
            "llm_guessed_date": "2023-01-01T00:00:00Z",
            "llm_extracted_dates": [{"date": "2023-01-01"}, {"date": "2023-01-02"}],
            "llm_model_name": "dummy-model",
            "llm_content_flavour": "dummy-flavour",
            "llm_tags": ["tag1", "tag2"]
        }

class DummyOpenAIManagerNoSummary:
    def get_chunks_and_embeddings(self, text):
        return [{"text_chunk": text, "vector": [1, 2, 3]}]
    def call_openai_api_text(self, text_content: str, domain_name: str) -> dict:
        return {}

class DummyOpenAIManagerWithException(DummyOpenAIManager):
    def call_openai_api_text(self, text_content: str, domain_name: str) -> dict:
        raise Exception("OpenAI API error")

# Dummy build_document to inject via monkeypatch.
def dummy_build_document(**kwargs):
    return kwargs

@pytest.fixture
def dummy_dependencies():
    archive_handler = DummyArchiveHandler()
    openai_manager = DummyOpenAIManager()
    return archive_handler, openai_manager

@pytest.fixture
def other_handler(dummy_dependencies, monkeypatch):
    archive_handler, openai_manager = dummy_dependencies
    # Patch ElasticsearchManager.build_document to simply return the kwargs.
    monkeypatch.setattr(ElasticsearchManager, "build_document", dummy_build_document)
    return OtherHandler(archive_handler=archive_handler, openai_manager=openai_manager)

@pytest.fixture
def other_handler_no_llm_summary(monkeypatch):
    archive_handler = DummyArchiveHandler()
    openai_manager = DummyOpenAIManagerNoSummary()
    monkeypatch.setattr(ElasticsearchManager, "build_document", dummy_build_document)
    return OtherHandler(archive_handler=archive_handler, openai_manager=openai_manager)

def test_process_other_file_success(tmp_path, other_handler):
    # Create a temporary file with known content.
    content = "This is a test content for other file."
    temp_file = tmp_path / "test_other.txt"
    temp_file.write_text(content, encoding="utf-8")
    file_path = str(temp_file)
    mime_type = "text/plain"
    domain_name = "dummy.domain"
    
    result = other_handler.process_other_file(file_path=file_path, mime_type=mime_type, domain_name=domain_name)
    assert isinstance(result, list)
    assert len(result) == 1
    doc = result[0]
    
    # Check that the document contains expected keys and values.
    expected_keys = {
        "id", "title", "thumbnail", "file_type", "mime_type", "text_full",
        "llm_summary", "llm_summary_vector", "llm_guessed_date", "llm_extracted_dates",
        "llm_model_name", "llm_content_flavour", "llm_tags", "domain_name",
        "url", "text", "alternate_url"
    }
    assert expected_keys.issubset(set(doc.keys()))
    # Verify some values.
    assert doc["id"] == file_path.replace(os.sep, "/")
    assert os.path.basename(file_path) == doc["title"]
    assert doc["thumbnail"] == "http://dummy.thumbnail/other"
    assert doc["mime_type"] == mime_type
    assert doc["text_full"] == content
    # Check the dummy responses from DummyOpenAIManager.
    assert doc["llm_summary"] == "dummy summary"
    assert doc["llm_summary_vector"] == [0.1, 0.2]
    assert doc["llm_guessed_date"] == "2023-01-01T00:00:00Z"
    assert doc["llm_extracted_dates"] == [{"date": "2023-01-01"}, {"date": "2023-01-02"}]
    assert doc["llm_model_name"] == "dummy-model"
    assert doc["llm_content_flavour"] == "dummy-flavour"
    assert doc["llm_tags"] == ["tag1", "tag2"]
    # Check that the chunks list is as expected.
    assert isinstance(doc["text"], list)
    assert len(doc["text"]) == 1
    chunk = doc["text"][0]
    assert chunk["text_chunk"] == content
    assert chunk["vector"] == [1, 2, 3]

def test_process_other_file_empty_url(tmp_path, monkeypatch):
    # Use DummyArchiveHandler which returns an empty URL.
    archive_handler = DummyArchiveHandlerEmptyURL()
    openai_manager = DummyOpenAIManager()
    # Patch build_document as before.
    monkeypatch.setattr(ElasticsearchManager, "build_document", dummy_build_document)
    handler = OtherHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a temporary file
    content = "Content that will not be processed due to empty URL."
    temp_file = tmp_path / "test_empty_url.txt"
    temp_file.write_text(content, encoding="utf-8")
    file_path = str(temp_file)
    result = handler.process_other_file(file_path=file_path)
    # Since URL is empty, a ValueError is raised and caught so result is None.
    assert result is None

def test_process_other_file_file_read_error(tmp_path, other_handler):
    # Provide a non-existing file path to trigger a file read error.
    non_existent_path = str(tmp_path / "non_existent_file.txt")
    result = other_handler.process_other_file(file_path=non_existent_path)
    # The exception is caught inside process_other_file so it should return None.
    assert result is None

def test_process_other_file_default_llm_summary(tmp_path, other_handler_no_llm_summary):
    # Create a temporary file with known content.
    content = "Test content for file with no LLM summary."
    temp_file = tmp_path / "test_no_llm_summary.txt"
    temp_file.write_text(content, encoding="utf-8")
    file_path = str(temp_file)
    mime_type = "text/plain"
    domain_name = "dummy.domain"
    
    result = other_handler_no_llm_summary.process_other_file(file_path=file_path, mime_type=mime_type, domain_name=domain_name)
    assert isinstance(result, list)
    assert len(result) == 1
    doc = result[0]
    
    # Check that the default placeholder is used for llm_summary.
    assert doc["llm_summary"] == "[ Still awaiting LLM Enrichment... ]"

def test_preprocess_other_file_success(other_handler):
    # Mock the DocumentConverter
    with patch('indexer.other_handler.DocumentConverter') as mock_converter_class:
        # Set up the mock converter instance and its convert method
        mock_converter_instance = MagicMock()
        mock_converter_class.return_value = mock_converter_instance
        
        # Mock the result object with document.export_to_markdown() method
        mock_result = MagicMock()
        mock_result.document.export_to_markdown.return_value = "Converted markdown content"
        mock_converter_instance.convert.return_value = mock_result
        
        # Call the method
        file_path = "path/to/test.docx"
        result = other_handler._preprocess_other_file(file_path)
        
        # Verify results
        assert result == "Converted markdown content"
        mock_converter_instance.convert.assert_called_once_with(file_path)
        mock_result.document.export_to_markdown.assert_called_once()

def test_preprocess_other_file_exception(other_handler):
    # Mock the DocumentConverter to raise an exception
    with patch('indexer.other_handler.DocumentConverter') as mock_converter_class:
        mock_converter_instance = MagicMock()
        mock_converter_class.return_value = mock_converter_instance
        mock_converter_instance.convert.side_effect = Exception("Conversion failed")
        
        # The method should raise an exception
        with pytest.raises(Exception, match="Conversion failed"):
            other_handler._preprocess_other_file("path/to/test.docx")

def test_process_other_file_openai_exception(tmp_path, dummy_dependencies, monkeypatch):
    # Set up handler with an OpenAI manager that raises an exception
    archive_handler, _ = dummy_dependencies
    openai_manager = DummyOpenAIManagerWithException()
    monkeypatch.setattr(ElasticsearchManager, "build_document", dummy_build_document)
    handler = OtherHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a temporary file with content
    content = "Test content for OpenAI exception handling"
    temp_file = tmp_path / "test_openai_exception.txt"
    temp_file.write_text(content, encoding="utf-8")
    file_path = str(temp_file)
    
    # Process the file - should not raise an exception despite OpenAI error
    result = handler.process_other_file(file_path=file_path, mime_type="text/plain")
    
    # Verify the document is created with default LLM values
    assert isinstance(result, list)
    assert len(result) == 1
    doc = result[0]
    assert doc["llm_summary"] == "[ Still awaiting LLM Enrichment... ]"
    assert "text" in doc  # Should still have chunks and embeddings

def test_process_other_file_general_exception(tmp_path, other_handler, monkeypatch):
    # Create a file that will be processed
    temp_file = tmp_path / "test_exception.txt"
    temp_file.write_text("Test content", encoding="utf-8")
    file_path = str(temp_file)
    
    # Patch the _extract_text_from_other_file method to raise an exception
    def mock_extract_text_raising_exception(*args, **kwargs):
        raise Exception("Simulated extraction error")
    
    monkeypatch.setattr(other_handler, "_extract_text_from_other_file", mock_extract_text_raising_exception)
    
    # The method should return None when an exception occurs
    result = other_handler.process_other_file(file_path=file_path)
    assert result is None

def test_skip_llm_enrichment_env_var(dummy_dependencies, monkeypatch):
    # Set the environment variable
    monkeypatch.setenv("SKIP_LLM_ENRICHMENT", "true")
    
    archive_handler, openai_manager = dummy_dependencies
    # Initialize handler with llm_enrichment_enabled=True, but env var should override it
    handler = OtherHandler(
        archive_handler=archive_handler, 
        openai_manager=openai_manager,
        llm_enrichment_enabled=True
    )
    
    # Check that the flag was set to False due to env var
    assert handler._LLM_ENRICHMENT_ENABLED is False
    
    # Test that LLM enrichment is actually skipped
    with patch.object(openai_manager, 'call_openai_api_text') as mock_openai_call:
        content = "Test content"
        temp_file_path = "test_file.txt"
        
        # Mock file read operations
        with patch('builtins.open', mock_open(read_data=content)):
            # Mock URL conversion
            with patch.object(archive_handler, '_convert_to_archive_url', return_value='http://example.com/test_file.txt'):
                monkeypatch.setattr(ElasticsearchManager, "build_document", dummy_build_document)
                handler.process_other_file(file_path=temp_file_path)
                
                # Verify that call_openai_api_text was not called
                mock_openai_call.assert_not_called()

def test_llm_enrichment_enabled_default(dummy_dependencies, monkeypatch):
    # Clear the environment variable if it exists
    monkeypatch.delenv("SKIP_LLM_ENRICHMENT", raising=False)
    
    archive_handler, openai_manager = dummy_dependencies
    # Initialize handler with default llm_enrichment_enabled
    handler = OtherHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Check that the flag is True by default
    assert handler._LLM_ENRICHMENT_ENABLED is True