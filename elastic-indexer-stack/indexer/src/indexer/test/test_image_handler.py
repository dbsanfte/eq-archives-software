import os
import pytest
from indexer.image_handler import ImageHandler
from indexer.es_manager import ElasticsearchManager

# Pseudocode:
#
# 1. Define dummy dependencies:
#    • DummyArchiveHandler with methods:
#         _convert_to_archive_url(file_path) → returns a valid URL, unless overridden;
#         _strip_index_html_from_url(url) → returns url with "index.html" removed;
#         _resolve_thumbnail_url(url, file_type) → returns a dummy thumbnail URL.
#    • DummyOpenAIManager with method:
#         call_openai_api_image(image_b64, mime_type, domain_name) → returns a dict with keys:
#            "llm_model_name", "llm_summary", "llm_image_text", "llm_content_flavour",
#            "llm_tags", "llm_image_text_vector".
#    • Patch ElasticsearchManager.build_document to simply return the passed parameters in a dict.
#
# 2. Write test functions:
#    • test_process_image_file_success:
#         - Create a temporary binary (image) file with dummy content.
#         - Instantiate ImageHandler with dummy dependencies.
#         - Call process_image_file with the dummy file path, full path, mime type, and domain name.
#         - Assert that it returns a list with one document and that the expected keys and values are present.
#    • test_process_image_file_file_error:
#         - Pass a non-existent file path to process_image_file.
#         - Assert that the function handles the file error gracefully (returns None).
#    • test_process_image_file_invalid_url:
#         - Create a dummy ArchiveHandler subclass where _convert_to_archive_url returns an empty string.
#         - Process a valid file and assert that exception is raised internally and the function returns None.
#
# 3. Use pytest and monkeypatch for patching ElasticsearchManager.build_document.
# 4. Use a relative import to import ImageHandler.
# 
# Now output the complete test file code in a single Python code block.

# Python

# Relative import from the package based on __init__.py being present in parent directory.

# Dummy dependency implementations
class DummyArchiveHandler:
    def _convert_to_archive_url(self, file_path: str) -> str:
        return "http://dummy.archive/" + file_path.replace(os.sep, "/")

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
    
    docs = handler.process_image_file(file_path, full_path, mime_type, domain_name)
    
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
    
    result = handler.process_image_file(file_path, fake_full_path, mime_type, domain_name)
    # Since an Exception is caught inside, result is None
    assert result is None

def test_process_image_file_invalid_url(tmp_path, dummy_dependencies, caplog):
    # Create a subclass of DummyArchiveHandler that returns an empty URL.
    class InvalidURLArchiveHandler(DummyArchiveHandler):
        def _convert_to_archive_url(self, file_path: str) -> str:
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
    
    result = handler.process_image_file(file_path, full_path, mime_type, domain_name)
    # Since _convert_to_archive_url returns empty string, ValueError is raised and caught, so result is None.
    assert result is None