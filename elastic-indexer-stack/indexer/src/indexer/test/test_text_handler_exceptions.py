import os
import json
import datetime
import pytest
from bs4 import BeautifulSoup
from indexer.text_handler import TextHandler
from langchain_core.documents import Document

# Dummy dependencies to help instantiate a TextHandler
class DummyArchiveHandler:
    def __init__(self):
        self._mailing_lists_path = "mailing_lists"
        self._newsgroups_path = "newsgroups"
        self._websites_path = "websites"

    def _convert_to_archive_url(self, relative_path: str) -> str:
        return "http://dummy.archive/" + relative_path.replace(os.sep, "/")

    def _strip_index_html_from_url(self, url: str) -> str:
        return url.replace("index.html", "")

    def _resolve_thumbnail_url(self, url: str, file_type: str) -> str:
        return "http://dummy.thumbnail/" + file_type

class DummyOpenAIManager:
    def get_chunks_and_embeddings(self, document: Document):
        # Simply return a single chunk using the document's page content.
        return [{
            "text_chunk": document.page_content,
            "vector": [0.1, 0.2, 0.3]
        }]

    def call_openai_api_text(self, text_content: str, domain_name: str) -> dict:
        return {
            "llm_summary": "dummy summary",
            "llm_summary_vector": [0.1, 0.2],
            "llm_guessed_date": "2023-01-01T00:00:00Z",
            "llm_extracted_dates": [{"date": "2023-01-01"}],
            "llm_model_name": "dummy-model",
            "llm_content_flavour": "dummy-flavour",
            "llm_tags": ["tag1", "tag2"]
        }

@pytest.fixture
def dummy_dependencies():
    archive_handler = DummyArchiveHandler()
    openai_manager = DummyOpenAIManager()
    return archive_handler, openai_manager

@pytest.fixture
def text_handler(dummy_dependencies):
    archive_handler, openai_manager = dummy_dependencies
    return TextHandler(archive_handler=archive_handler, openai_manager=openai_manager)

# ==========================
# Tests for _preprocess_website_file
# ==========================

def test_preprocess_website_file_utf8(tmp_path, text_handler):
    # Create a temporary website file encoded in UTF-8.
    file_content = "<html><head><title>Website Title</title></head><body>Website Content</body></html>"
    website_dir = tmp_path / "websites"
    website_dir.mkdir()
    test_file = website_dir / "page.html"
    test_file.write_text(file_content, encoding="utf-8")

    result = text_handler._preprocess_website_file(full_path=str(test_file), relative_path="websites/page.html")
    # Check that the returned markdown includes the dummy-converted URL and shows the title & content.
    assert "Page URL:" in result or "http://dummy.archive/" in result
    assert "Website Title" in result
    assert "Website Content" in result

def test_preprocess_website_file_cp1252(tmp_path, text_handler):
    # Create a temporary website file encoded in cp1252 with characters that would fail in UTF-8.
    file_content = "<html><head><title>Œuvre</title></head><body>Résumé</body></html>"
    website_dir = tmp_path / "websites"
    website_dir.mkdir()
    test_file = website_dir / "page_cp1252.html"
    test_file.write_text(file_content, encoding="cp1252")

    result = text_handler._preprocess_website_file(full_path=str(test_file), relative_path="websites/page_cp1252.html")
    # Validate that content from a cp1252 file is correctly read.
    assert "Œuvre" in result or "Oeuvre" in result  # allow for minor conversion differences
    assert "Résumé" in result

def test_preprocess_website_file_decode_failure(tmp_path, text_handler, monkeypatch):
    # Create a file that fails to decode in both UTF-8 and cp1252.
    website_dir = tmp_path / "websites"
    website_dir.mkdir()
    test_file = website_dir / "bad_file.html"
    test_file.write_bytes(b"\xff\xfe\xfa\xfb")

    # Monkey-patch open within the module to always raise UnicodeDecodeError.
    def fake_open(*args, **kwargs):
        raise UnicodeDecodeError("dummy", b"", 0, 1, "fake error")
    monkeypatch.setattr("builtins.open", fake_open)

    with pytest.raises(ValueError, match="Could not decode file"):
        text_handler._preprocess_website_file(full_path=str(test_file), relative_path="websites/bad_file.html")
    # monkeypatch automatically undoes changes after the test completes

# ==========================
# Tests for process_text_file
# ==========================

def test_process_text_file_default(tmp_path, text_handler):
    # Create a generic HTML file that does not belong to a special archive folder.
    file_content = "<h1>Generic File</h1><p>Content here.</p>"
    test_file = tmp_path / "generic.html"
    test_file.write_text(file_content, encoding="utf-8")

    docs = text_handler.process_text_file(relative_path="generic.html", full_path=str(test_file),
                                            mime_type="text/html", domain_name="example.com")
    assert isinstance(docs, list)
    assert len(docs) == 1
    doc = docs[0]
    # Validate that markdown conversion occurred and the dummy URL converter was used.
    assert "Generic File" in doc["text"][0]["text_chunk"]
    assert doc["url"].startswith("http://dummy.archive/")

def test_process_text_file_llm_exception(tmp_path, text_handler, monkeypatch):
    # Create a simple text file.
    file_content = "Test LLM error handling."
    test_file = tmp_path / "error.txt"
    test_file.write_text(file_content, encoding="utf-8")

    # Simulate an exception during LLM enrichment.
    def fake_call_openai_api_text(*args, **kwargs):
        raise Exception("Simulated LLM error")
    monkeypatch.setattr(text_handler._openai_manager, "call_openai_api_text", fake_call_openai_api_text)

    docs = text_handler.process_text_file(relative_path="generic/error.txt", full_path=str(test_file),
                                            mime_type="text/plain", domain_name="example.com")
    # With LLM error, the summary should default to the placeholder.
    assert docs[0]["llm_summary"] == "[ Still awaiting LLM Enrichment... ]"

def test_process_text_file_nonexistent_file(tmp_path, text_handler):
    # Call process_text_file with a path that does not exist.
    fake_file = tmp_path / "nonexistent.txt"
    with pytest.raises(Exception):
        text_handler.process_text_file(relative_path="nonexistent.txt", full_path=str(fake_file),
                                       mime_type="text/plain", domain_name="example.com")

# ==========================
# Tests for _resolve_title_from_file
# ==========================

def test_resolve_title_website_with_title(tmp_path, text_handler):
    # Test website branch with a valid <title> tag.
    file_content = "<html><head><title>Website Proper Title</title></head><body>Body</body></html>"
    website_dir = tmp_path / "websites"
    website_dir.mkdir()
    test_file = website_dir / "site.html"
    test_file.write_text(file_content, encoding="utf-8")

    title = text_handler._resolve_title_from_file(full_path=str(test_file), relative_path="websites/site.html")
    assert title == "Website Proper Title"

def test_resolve_title_website_without_title(tmp_path, text_handler):
    # Test website branch when no <title> tag is present.
    file_content = "<html><head></head><body>No title</body></html>"
    website_dir = tmp_path / "websites"
    website_dir.mkdir()
    test_file = website_dir / "notitle.html"
    test_file.write_text(file_content, encoding="utf-8")

    title = text_handler._resolve_title_from_file(full_path=str(test_file), relative_path="websites/notitle.html")
    assert title == "Untitled"

def test_resolve_title_newsgroup(tmp_path, text_handler):
    # Test newsgroup branch: file in newsgroups folder containing a Subject line.
    file_content = "Subject: Newsgroup Title\nOther-Header: value\n\nBody content"
    newsgroups_dir = tmp_path / "newsgroups"
    newsgroups_dir.mkdir()
    test_file = newsgroups_dir / "post.txt"
    test_file.write_text(file_content, encoding="utf-8")

    title = text_handler._resolve_title_from_file(full_path=str(test_file), relative_path="newsgroups/post.txt")
    assert title == "Newsgroup Title"

def test_resolve_title_newsgroup_missing_subject(tmp_path, text_handler, caplog):
    # Test newsgroup branch when no Subject header is found.
    file_content = "Header: test\nAnother: value\n\nBody content"
    newsgroups_dir = tmp_path / "newsgroups"
    newsgroups_dir.mkdir()
    test_file = newsgroups_dir / "post_no_subject.txt"
    test_file.write_text(file_content, encoding="utf-8")

    title = text_handler._resolve_title_from_file(full_path=str(test_file), relative_path="newsgroups/post_no_subject.txt")
    assert title == "Untitled"
    # Verify warning is logged about the missing subject.
    assert any("Couldn't find subject line" in record.message for record in caplog.records)

def test_resolve_title_mailing_list(tmp_path, text_handler):
    # Test mailing list branch: JSON file with a valid subject field.
    mailing_list_data = {
        "ygData": {
            "subject": "Mailing List Title",
            "from": "sender@example.com",
            "date": 1609459200,
            "messageBody": "<p>The message body</p>"
        }
    }
    mailing_list_dir = tmp_path / "mailing_lists" / "list1"
    mailing_list_dir.mkdir(parents=True)
    test_file = mailing_list_dir / "mailing_list.json"
    test_file.write_text(json.dumps(mailing_list_data), encoding="utf-8")

    title = text_handler._resolve_title_from_file(full_path=str(test_file),
                                                   relative_path="mailing_lists/list1/mailing_list.json")
    assert title == "Mailing List Title"

def test_resolve_title_default(tmp_path, text_handler):
    # Test the default branch for files outside the recognized folders.
    file_content = "Some random content."
    test_file = tmp_path / "random.txt"
    test_file.write_text(file_content, encoding="utf-8")

    title = text_handler._resolve_title_from_file(full_path=str(test_file), relative_path="random.txt")
    # Should return the basename as default.
    assert title == "random.txt"

def test_resolve_title_website_decode_failure(tmp_path, text_handler, monkeypatch, caplog):
    # Test website branch when decoding fails in both UTF-8 and cp1252.
    website_dir = tmp_path / "websites"
    website_dir.mkdir()
    test_file = website_dir / "bad_encoding.html"
    test_file.write_bytes(b"\xff\xfe\xfa\xfb")

    def fake_open(*args, **kwargs):
        raise UnicodeDecodeError("dummy", b"", 0, 1, "fake error")
    monkeypatch.setattr("builtins.open", fake_open)

    title = text_handler._resolve_title_from_file(full_path=str(test_file), relative_path="websites/bad_encoding.html")
    # Should fallback to the default title "Untitled" and log a warning.
    assert title == "Untitled"
    assert any("Could not decode file" in record.message or "Using default title" in record.message for record in caplog.records)
    # monkeypatch automatically undoes changes after the test completes