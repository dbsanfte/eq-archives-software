import os
import pytest
from indexer.image_handler import ImageHandler
from indexer.es_manager import ElasticsearchManager
from indexer.openai_manager import OpenAIManager

# Dummy dependency implementations
class DummyArchiveHandler:
    def _convert_to_archive_url(self, relative_path: str) -> str:
        return "http://dummy.archive/" + relative_path.replace(os.sep, "/")

    def _strip_index_html_from_url(self, url: str) -> str:
        return url.replace("index.html", "")

    def _resolve_thumbnail_url(self, url: str, file_type: str) -> str:
        return "http://dummy.thumbnail/" + file_type

class DummyOpenAIManager:
    def call_openai_api_image(self, image_b64, mime_type, domain_name) -> dict:
        return {
            "llm_model_name": "dummy-model",
            "llm_summary": "dummy summary",
            "llm_image_text": ["dummy text"],
            "llm_content_flavour": "dummy flavour",
            "llm_tags": ["tag1", "tag2"],
            "llm_image_text_vector": [0.1, 0.2],
        }

class DummyOpenAIManagerNoSummary:
    def call_openai_api_image(self, image_b64, mime_type, domain_name) -> dict:
        # Return dict without the "llm_summary" key to trigger default placeholder.
        return {
            "llm_model_name": "dummy-model",
            "llm_image_text": ["dummy text"],
            "llm_content_flavour": "dummy flavour",
            "llm_tags": ["tag1"],
            "llm_image_text_vector": [0.1, 0.2],
        }

# Patch ElasticsearchManager.build_document to simply return the parameters passed in
def dummy_build_document(**kwargs):
    return kwargs

@pytest.fixture(autouse=True)
def patch_es_manager(monkeypatch):
    monkeypatch.setattr(ElasticsearchManager, "build_document", dummy_build_document)

@pytest.fixture
def dummy_dependencies():
    archive_handler = DummyArchiveHandler()
    openai_manager = DummyOpenAIManager()
    return archive_handler, openai_manager

def test_process_image_file_success(tmp_path, dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    handler = ImageHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a temporary binary image file with dummy content
    img_content = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
    temp_image = tmp_path / "test_image.png"
    temp_image.write_bytes(img_content)
    
    file_path = os.path.join("images", "test_image.png")
    full_path = str(temp_image)
    mime_type = "image/png"
    domain_name = "dummy.domain"
    
    docs = handler.process_image_file(relative_path=file_path, full_path=full_path, 
                                      mime_type=mime_type, domain_name=domain_name)
    
    assert isinstance(docs, list)
    assert len(docs) == 1
    doc = docs[0]
    
    # Verify that build_document returns the document with our expected keys.
    expected_keys = {"id", "title", "thumbnail", "file_type", "mime_type", "domain_name",
                     "url", "alternate_url", "llm_model_name", "llm_summary", "llm_image_text",
                     "llm_content_flavour", "llm_tags", "llm_image_text_vector"}
    assert expected_keys.issubset(doc.keys())
    # Validate some key values
    assert doc["title"] == "test_image.png"
    assert doc["url"] == "http://dummy.archive/" + file_path.replace(os.sep, "/")
    thumb_expected = "http://dummy.thumbnail/image"
    assert doc["thumbnail"] == thumb_expected
    # Validate the openai response parts
    assert doc["llm_model_name"] == "dummy-model"
    assert doc["llm_summary"] == "dummy summary"
    assert doc["llm_image_text"] == ["dummy text"]
    assert doc["llm_tags"] == ["tag1", "tag2"]

def test_process_image_file_file_error(tmp_path, dummy_dependencies, caplog):
    archive_handler, openai_manager = dummy_dependencies
    handler = ImageHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Use a non-existent full_path to simulate file read error
    fake_full_path = str(tmp_path / "non_existent.png")
    file_path = os.path.join("images", "non_existent.png")
    mime_type = "image/png"
    domain_name = "dummy.domain"
    
    result = handler.process_image_file(relative_path=file_path, full_path=fake_full_path, 
                                        mime_type=mime_type, domain_name=domain_name)
    # Since an Exception is caught inside, result is None
    assert result is None

def test_process_image_file_invalid_url(tmp_path, dummy_dependencies, caplog):
    # Create a subclass of DummyArchiveHandler that returns an empty URL.
    class InvalidURLArchiveHandler(DummyArchiveHandler):
        def _convert_to_archive_url(self, relative_path: str) -> str:
            return ""
    
    archive_handler = InvalidURLArchiveHandler()
    openai_manager = DummyOpenAIManager()
    handler = ImageHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a temporary binary image file with dummy content
    img_content = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
    temp_image = tmp_path / "test_image_invalid.png"
    temp_image.write_bytes(img_content)
    
    file_path = os.path.join("images", "test_image_invalid.png")
    full_path = str(temp_image)
    mime_type = "image/png"
    domain_name = "dummy.domain"
    
    result = handler.process_image_file(relative_path=file_path, full_path=full_path, mime_type=mime_type, domain_name=domain_name)
    # Since _convert_to_archive_url returns empty string, ValueError is raised and caught, so result is None.
    assert result is None
    # Check for the error message - using a more general check that will match various formats of the same message
    assert "URL not found" in caplog.text or "archive URL" in caplog.text.lower()

def test_process_image_file_placeholder_llm_summary(tmp_path):
    archive_handler = DummyArchiveHandler()
    openai_manager = DummyOpenAIManagerNoSummary()
    handler = ImageHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a temporary binary image file with dummy content (JPEG header)
    img_content = b'\xFF\xD8\xFF\xE0'
    temp_image = tmp_path / "test_image.jpg"
    temp_image.write_bytes(img_content)
    
    file_path = os.path.join("images", "test_image.jpg")
    full_path = str(temp_image)
    mime_type = "image/jpeg"
    domain_name = "dummy.domain"
    
    docs = handler.process_image_file(relative_path=file_path, full_path=full_path,
                                       mime_type=mime_type, domain_name=domain_name)
    
    # Assert that docs is a list with one document
    assert isinstance(docs, list)
    assert len(docs) == 1
    doc = docs[0]
    
    # Verify that if "llm_summary" isn't defined, the placeholder default is used.
    assert doc["llm_summary"] == OpenAIManager.AWAITING_LLM_ENRICHMENT
    
def test_llm_enrichment_disabled_constructor(tmp_path, dummy_dependencies, caplog):
    archive_handler, openai_manager = dummy_dependencies
    
    # Create a spy version of OpenAIManager to verify it's not called
    class SpyOpenAIManager(DummyOpenAIManager):
        def __init__(self):
            self.called = False
            
        def call_openai_api_image(self, image_b64, mime_type, domain_name):
            self.called = True
            return super().call_openai_api_image(image_b64, mime_type, domain_name)
    
    spy_openai = SpyOpenAIManager()
    
    # Create handler with LLM enrichment disabled
    handler = ImageHandler(
        archive_handler=archive_handler,
        openai_manager=spy_openai,
        llm_enrichment_enabled=False
    )
    
    # Create a temporary image file
    img_content = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
    temp_image = tmp_path / "test_image.png"
    temp_image.write_bytes(img_content)
    
    file_path = os.path.join("images", "test_image.png")
    full_path = str(temp_image)
    mime_type = "image/png"
    domain_name = "dummy.domain"
    
    docs = handler.process_image_file(relative_path=file_path, full_path=full_path, 
                                        mime_type=mime_type, domain_name=domain_name)
    
    # Verify OpenAI was not called
    assert not spy_openai.called
    
    # Verify document was still created with placeholder values
    assert isinstance(docs, list)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["llm_summary"] == OpenAIManager.AWAITING_LLM_ENRICHMENT
    assert doc["llm_image_text"] == None
    assert doc["llm_tags"] == None
    assert doc["llm_image_text_vector"] == None

def test_llm_enrichment_disabled_env_var(tmp_path, dummy_dependencies, caplog, monkeypatch):
    archive_handler, openai_manager = dummy_dependencies
    
    # Create a spy version of OpenAIManager
    class SpyOpenAIManager(DummyOpenAIManager):
        def __init__(self):
            self.called = False
            
        def call_openai_api_image(self, image_b64, mime_type, domain_name):
            self.called = True
            return super().call_openai_api_image(image_b64, mime_type, domain_name)
    
    spy_openai = SpyOpenAIManager()
    
    # Set environment variable to disable LLM enrichment
    monkeypatch.setenv("SKIP_LLM_ENRICHMENT", "true")
    
    # Create handler (should respect the env var)
    handler = ImageHandler(
        archive_handler=archive_handler,
        openai_manager=spy_openai
    )
    
    # Create a temporary image file
    img_content = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
    temp_image = tmp_path / "test_image.png"
    temp_image.write_bytes(img_content)
    
    file_path = os.path.join("images", "test_image.png")
    full_path = str(temp_image)
    mime_type = "image/png"
    domain_name = "dummy.domain"
    
    docs = handler.process_image_file(relative_path=file_path, full_path=full_path, 
                                        mime_type=mime_type, domain_name=domain_name)
    
    # Verify OpenAI was not called
    assert not spy_openai.called
    
    # Verify document was still created with placeholder values
    assert isinstance(docs, list)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["llm_summary"] == OpenAIManager.AWAITING_LLM_ENRICHMENT
    assert doc["llm_image_text"] == None
    assert doc["llm_tags"] == None
    assert doc["llm_image_text_vector"] == None

def test_openai_api_exception(tmp_path, caplog):
    archive_handler = DummyArchiveHandler()
    
    # Create OpenAI manager that raises an exception
    class ExceptionOpenAIManager:
        def call_openai_api_image(self, image_b64, mime_type, domain_name):
            raise Exception("API failure")
    
    openai_manager = ExceptionOpenAIManager()
    handler = ImageHandler(archive_handler=archive_handler, openai_manager=openai_manager)
    
    # Create a temporary image file
    img_content = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
    temp_image = tmp_path / "test_image.png"
    temp_image.write_bytes(img_content)
    
    file_path = os.path.join("images", "test_image.png")
    full_path = str(temp_image)
    mime_type = "image/png"
    domain_name = "dummy.domain"
    
    docs = handler.process_image_file(relative_path=file_path, full_path=full_path, 
                                        mime_type=mime_type, domain_name=domain_name)
    
    # Verify document was still created despite OpenAI exception
    assert isinstance(docs, list)
    assert len(docs) == 1
    doc = docs[0]
    
    # Verify placeholder values were used
    assert doc["llm_model_name"] is None
    assert doc["llm_summary"] == OpenAIManager.AWAITING_LLM_ENRICHMENT
    assert doc["llm_image_text"] == None
    assert doc["llm_tags"] == None
    
    # Verify error was logged
    assert "Error calling OpenAI API for image" in caplog.text
    assert "API failure" in caplog.text
    assert "Skipping LLM enrichment for this image" in caplog.text