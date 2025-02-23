import os
import pytest
import magic
from unittest.mock import MagicMock
from indexer.indexer import Indexer

# Dummy dependency implementations for testing
class DummyArchiveHandler:
    def _extract_domain_and_date(self, relative_path, full_path):
        return ("example.com", "2022-01-01")

class DummyTextHandler:
    def process_text_file(self, file_path, full_path, mime_type, domain_name):
        # Simulate generating one document
        return [{"id": "doc_text", "content": "dummy text content"}]

class DummyImageHandler:
    def process_image_file(self, file_path, full_path, mime_type, domain_name):
        return [{"id": "doc_image", "content": "dummy image content"}]

class DummyOtherHandler:
    def process_other_file(self, file_path, mime_type, domain_name):
        return [{"id": "doc_other", "content": "dummy other content"}]

# Fake functions to simulate file system and mime detection behavior
def fake_isfile_true(path):
    return True

def fake_isfile_false(path):
    return False

def fake_magic_from_file_text(full_path, mime=True):
    return "text/plain"

def fake_magic_from_file_image(full_path, mime=True):
    return "image/jpeg"

def fake_magic_from_file_other(full_path, mime=True):
    return "application/pdf"

@pytest.fixture
def dummy_dependencies(monkeypatch):
    # Create dummy ElasticsearchManager and OpenAIManager
    dummy_es_manager = MagicMock()
    dummy_es_manager.index_document = MagicMock()
    dummy_openai_manager = MagicMock()

    dummy_archive_handler = DummyArchiveHandler()
    indexer = Indexer(dummy_es_manager, dummy_openai_manager, dummy_archive_handler)
    # Override the file type handlers with our dummy implementations
    indexer._text_handler = DummyTextHandler()
    indexer._image_handler = DummyImageHandler()
    indexer._other_handler = DummyOtherHandler()

    # Set local repo path to current working directory for predictable behavior
    monkeypatch.setenv("LOCAL_REPO_PATH", os.getcwd())
    return indexer, dummy_es_manager

def test_missing_file_path(dummy_dependencies):
    indexer, _ = dummy_dependencies
    with pytest.raises(ValueError, match="No file_path in RabbitMQ message!"):
        indexer.process_file({})

def test_file_not_found(dummy_dependencies, monkeypatch):
    indexer, _ = dummy_dependencies
    # Force os.path.isfile to return False
    monkeypatch.setattr(os.path, "isfile", fake_isfile_false)
    message = {"file_path": "nonexistent.txt"}
    with pytest.raises(ValueError, match="File not found on disk"):
        indexer.process_file(message)

def test_process_text_file(dummy_dependencies, monkeypatch):
    indexer, dummy_es_manager = dummy_dependencies
    monkeypatch.setattr(os.path, "isfile", fake_isfile_true)
    # Patch magic.from_file to return a text mime type
    monkeypatch.setattr(magic, "from_file", fake_magic_from_file_text)
    # Ensure SKIP_TEXT_FILES flag is not set to skip processing
    monkeypatch.delenv("SKIP_TEXT_FILES", raising=False)
    message = {"file_path": "dummy.txt"}
    indexer.process_file(message)
    # The DummyTextHandler returns one document with id "doc_text"
    dummy_es_manager.index_document.assert_called_with("doc_text", {"id": "doc_text", "content": "dummy text content", 'capture_date': '2022-01-01T00:00:00'})

def test_skip_text_file(dummy_dependencies, monkeypatch):
    indexer, dummy_es_manager = dummy_dependencies
    # Set SKIP_TEXT_FILES to 'true' so that text files are skipped
    monkeypatch.setenv("SKIP_TEXT_FILES", "true")
    monkeypatch.setattr(os.path, "isfile", fake_isfile_true)
    monkeypatch.setattr(magic, "from_file", fake_magic_from_file_text)
    message = {"file_path": "dummy.txt"}
    # Since the text file gets skipped, no indexing should occur.
    indexer.process_file(message)
    dummy_es_manager.index_document.assert_not_called()

def test_process_image_file(dummy_dependencies, monkeypatch):
    indexer, dummy_es_manager = dummy_dependencies
    monkeypatch.setattr(os.path, "isfile", fake_isfile_true)
    # Patch magic.from_file to return an image mime type
    monkeypatch.setattr(magic, "from_file", fake_magic_from_file_image)
    # Ensure SKIP_IMAGE_FILES flag is not set
    monkeypatch.delenv("SKIP_IMAGE_FILES", raising=False)
    message = {"file_path": "image.jpg"}
    indexer.process_file(message)
    dummy_es_manager.index_document.assert_called_with("doc_image", {"id": "doc_image", "content": "dummy image content", 'capture_date': '2022-01-01T00:00:00'})

def test_process_other_file(dummy_dependencies, monkeypatch):
    indexer, dummy_es_manager = dummy_dependencies
    monkeypatch.setattr(os.path, "isfile", fake_isfile_true)
    # Patch magic.from_file to return a mime type that does not start with 'text' or 'image'
    monkeypatch.setattr(magic, "from_file", fake_magic_from_file_other)
    monkeypatch.delenv("SKIP_OTHER_FILES", raising=False)
    message = {"file_path": "document.pdf"}
    indexer.process_file(message)
    dummy_es_manager.index_document.assert_called_with("doc_other", {"id": "doc_other", "content": "dummy other content", 'capture_date': '2022-01-01T00:00:00'})