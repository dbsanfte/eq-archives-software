import os
import tempfile
import pytest
from indexer.archive_handler import ArchiveHandler

@pytest.fixture
def archive_handler(monkeypatch):
    # Set environment variables for consistent paths
    monkeypatch.setenv("NEWSGROUPS_PATH", "newsgroups")
    monkeypatch.setenv("MAILING_LISTS_PATH", "mailing-lists")
    monkeypatch.setenv("WEBSITES_PATH", "websites")
    return ArchiveHandler()

def test_strip_index_html_from_url(archive_handler):
    url = "http://example.com/index.html"
    expected = "http://example.com"
    assert archive_handler._strip_index_html_from_url(url) == expected

    url2 = "http://example.com/page.html"
    # No change should be made if '/index.html' is not at the end
    assert archive_handler._strip_index_html_from_url(url2) == url2

def test_convert_to_archive_url_websites(archive_handler):
    # File path: websites/example.com/20220101132501/page.html
    file_path = os.path.join("websites", "example.com", "20220101132501", "page.html")
    expected = "https://web.archive.org/web/20220101132501/http://example.com/page.html"
    assert archive_handler._convert_to_archive_url(file_path) == expected

def test_convert_to_archive_url_mailing_lists(archive_handler):
    file_path = os.path.join("mailing-lists", "list1", "35.json")
    expected = "https://dbsanfte.github.io/eq-archives/mailing-lists/list1/html/35.html"
    assert archive_handler._convert_to_archive_url(file_path) == expected

def test_convert_to_archive_url_newsgroups(archive_handler):
    file_path = os.path.join("newsgroups", "message.txt")
    expected = "https://dbsanfte.github.io/eq-archives/newsgroups/message.txt"
    assert archive_handler._convert_to_archive_url(file_path) == expected

def test_extract_domain_and_date_websites(archive_handler):
    # Test extraction for websites using relative path
    relative_path = os.path.join("websites", "example.com", "20220101132501", "extra", "page.html")
    domain, capture_date = archive_handler._extract_domain_and_date(relative_path, "")
    assert domain == "example.com"
    assert capture_date == "20220101132501"

def test_extract_newsgroups(tmp_path, archive_handler):
    # Create a temporary file with a "Date: ..." line for newsgroups.
    content = "Header info\nDate: 2022-01-01\nRemaining content\n"
    test_file = tmp_path / "newsgroup.txt"
    test_file.write_text(content)
    full_path = str(test_file)
    
    domain, capture_date = archive_handler._extract_newsgroups(full_path)
    assert domain == "groups.google.com"
    assert capture_date == "2022-01-01"

def test_extract_mailing_lists(tmp_path, archive_handler):
    # Create a temporary file with a <b>Date:</b> tag for mailing lists.
    content = "<html>\n<b>Date:</b> 2022-02-02 <br/>\nOther content\n</html>"
    test_file = tmp_path / "mailing_list.txt"
    test_file.write_text(content)
    full_path = str(test_file)
    
    domain, capture_date = archive_handler._extract_mailing_lists(full_path)
    assert domain == "groups.yahoo.com"
    assert capture_date == "2022-02-02"

def test_resolve_thumbnail_url_defaults(archive_handler):
    # Test defaults when URL is None.
    assert archive_handler._resolve_thumbnail_url(None, "image") == "thumbnails/other-image.webp"
    assert archive_handler._resolve_thumbnail_url(None, "text") == "thumbnails/other.webp"

def test_resolve_thumbnail_url_web_archive(archive_handler):
    # URL contains web.archive.org and specific domains.
    url = "https://web.archive.org/web/20220101/http://castersrealm.com/page.html"
    assert archive_handler._resolve_thumbnail_url(url, "text") == "thumbnails/castersrealm.webp"
    
    url2 = "https://web.archive.org/web/20220101/http://allakhazam.com/page.html"
    assert archive_handler._resolve_thumbnail_url(url2, "text") == "thumbnails/allakhazam.webp"
    
    url3 = "https://web.archive.org/web/20220101/http://example.com/page.html"
    assert archive_handler._resolve_thumbnail_url(url3, "image") == "thumbnails/website-image.webp"
    assert archive_handler._resolve_thumbnail_url(url3, "text") == "thumbnails/website.webp"

def test_resolve_thumbnail_url_mailing_newsgroups(archive_handler):
    # Test for URLs indicating mailing lists and newsgroups.
    mailing_url = "http://example.com/mailing-lists/message.html"
    newsgroup_url = "http://example.com/newsgroups/message.html"
    assert archive_handler._resolve_thumbnail_url(mailing_url, "text") == "thumbnails/mailing-list.webp"
    assert archive_handler._resolve_thumbnail_url(newsgroup_url, "text") == "thumbnails/newsgroup.webp"