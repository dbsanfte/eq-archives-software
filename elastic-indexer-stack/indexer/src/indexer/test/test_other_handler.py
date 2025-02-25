import os
import pytest

from indexer.other_handler import OtherHandler
from indexer.es_manager import ElasticsearchManager

# Dummy implementations
class DummyArchiveHandler:
    def _convert_to_archive_url(self, file_path: str) -> str:
        return "http://dummy.archive/" + file_path.replace(os.sep, "/")
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