import os
import tempfile
import pytest
import json
from indexer.archive_handler import ArchiveHandler

@pytest.fixture
def archive_handler(monkeypatch):
    return ArchiveHandler()

def test_strip_index_html_from_url(archive_handler):
    url = "http://example.com/index.html"
    expected = "http://example.com"
    assert archive_handler._strip_index_html_from_url(url) == expected

    url2 = "http://example.com/page.html"
    # No change should be made if '/index.html' is not at the end
    assert archive_handler._strip_index_html_from_url(url2) == url2

def test_convert_to_archive_url_websites(archive_handler):
    # File path: websites/eq.castersrealm.com/20000612004545/cgi-bin/eq/postings.cgi
    file_path = os.path.join("websites", "eq.castersrealm.com", "20000612004545", "cgi-bin", "eq", "postings.cgi")
    expected = "https://web.archive.org/web/20000612004545/http://eq.castersrealm.com/cgi-bin/eq/postings.cgi"
    assert archive_handler._convert_to_archive_url(file_path) == expected

def test_convert_to_archive_url_websites_custom(archive_handler):
    # File path: websites/eq.castersrealm.com/20020322082233/players/default.asp?Action=&Letter=M&Searchtype=levels&Server=24&class=6&lower_level=1&upper_level=9
    file_path = os.path.join("websites", "eq.castersrealm.com", "20020322082233", "players", "default.asp?Action=&Letter=M&Searchtype=levels&Server=24&class=6&lower_level=1&upper_level=9")
    expected = "https://web.archive.org/web/20020322082233/http://eq.castersrealm.com/players/default.asp?Action=&Letter=M&Searchtype=levels&Server=24&class=6&lower_level=1&upper_level=9"
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
    assert capture_date == "2022-01-01 13:25:01"  # Updated to expect formatted date

def test_extract_newsgroups(tmp_path, archive_handler):
    # Create a temporary file with a "Date: ..." line for newsgroups.
    content = "Header info\nDate: 2022-01-01\nRemaining content\n"
    test_file = tmp_path / "newsgroup.txt"
    test_file.write_text(content)
    full_path = str(test_file)
    
    domain, capture_date = archive_handler._extract_newsgroups(full_path)
    assert domain == "groups.google.com"
    assert capture_date == "2022-01-01"

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

def test_extract_websites_date_formatting(archive_handler):
    # Test the direct method with various date formats
    relative_path = os.path.join("websites", "example.com", "20220101132501", "extra", "page.html")
    domain, formatted_date = archive_handler._extract_websites(relative_path=relative_path)
    assert domain == "example.com"
    assert formatted_date == "2022-01-01 13:25:01"
    
    # Test with another timestamp
    relative_path = os.path.join("websites", "test-domain.org", "19991231235959", "extra", "page.html")
    domain, formatted_date = archive_handler._extract_websites(relative_path=relative_path)
    assert domain == "test-domain.org"
    assert formatted_date == "1999-12-31 23:59:59"

# New tests for _extract_domain_and_date method branches
def test_extract_domain_and_date_newsgroups_path(archive_handler):
    # Test extraction for newsgroups using relative path
    relative_path = os.path.join("newsgroups", "message.txt")
    full_path = os.path.join("/root", "newsgroups", "message.txt")
    
    # Mock behavior for testing - we're checking the routing logic
    def mock_extract(path):
        return "mock-domain.com", "mock-date"
    
    original_extract = archive_handler._extract_newsgroups
    archive_handler._extract_newsgroups = mock_extract
    
    try:
        domain, date = archive_handler._extract_domain_and_date(relative_path, full_path)
        assert domain == "mock-domain.com"
        assert date == "mock-date"
    finally:
        archive_handler._extract_newsgroups = original_extract

def test_extract_domain_and_date_mailing_lists_path(archive_handler):
    # Test extraction for mailing lists using relative path
    relative_path = os.path.join("mailing-lists", "list1", "35.json")
    full_path = os.path.join("/root", "mailing-lists", "list1", "35.json")
    
    # Mock behavior for testing - we're checking the routing logic
    def mock_extract(path):
        return "mock-domain.com", "mock-date"
    
    original_extract = archive_handler._extract_mailing_lists
    archive_handler._extract_mailing_lists = mock_extract
    
    try:
        domain, date = archive_handler._extract_domain_and_date(relative_path, full_path)
        assert domain == "mock-domain.com"
        assert date == "mock-date"
    finally:
        archive_handler._extract_mailing_lists = original_extract

def test_extract_domain_and_date_unknown_path(archive_handler):
    # Test extraction for unknown path type
    relative_path = os.path.join("unknown", "file.txt")
    full_path = os.path.join("/root", "unknown", "file.txt")
    
    domain, date = archive_handler._extract_domain_and_date(relative_path, full_path)
    assert domain is None
    assert date is None

# Tests for _is_newsgroups method
def test_is_newsgroups(archive_handler):
    # Valid newsgroups path
    assert archive_handler._is_newsgroups(["newsgroups", "file.txt"]) is True
    
    # Non-newsgroups path
    assert archive_handler._is_newsgroups(["websites", "file.txt"]) is False
    
    # Empty path
    assert archive_handler._is_newsgroups([]) is False

# Tests for _is_mailing_lists method
def test_is_mailing_lists(archive_handler):
    # Valid mailing lists path
    assert archive_handler._is_mailing_lists(["mailing-lists", "list1", "35.json"]) is True
    
    # Non-mailing lists path
    assert archive_handler._is_mailing_lists(["websites", "file.txt"]) is False
    
    # Empty path
    assert archive_handler._is_mailing_lists([]) is False

# Tests for _resolve_thumbnail_url with different file types
def test_resolve_thumbnail_url_file_types(archive_handler):
    # Test with crgaming.com domain (should use castersrealm thumbnail)
    url = "https://web.archive.org/web/20220101/http://crgaming.com/page.html"
    assert archive_handler._resolve_thumbnail_url(url, "text") == "thumbnails/castersrealm.webp"
    
    # Test with unknown file type (should use other.webp)
    url = "https://web.archive.org/web/20220101/http://example.com/page.html"
    assert archive_handler._resolve_thumbnail_url(url, "unknown") == "thumbnails/other.webp"
    
    # Test with regular URL and image type
    url = "https://example.com/image.png"
    assert archive_handler._resolve_thumbnail_url(url, "image") == "thumbnails/other-image.webp"

# Exception handling tests for _extract_newsgroups method
def test_extract_newsgroups_file_not_found(archive_handler):
    # Test with non-existent file
    domain, date = archive_handler._extract_newsgroups("non_existent_file.txt")
    assert domain == "groups.google.com"
    assert date is None

def test_extract_newsgroups_no_date(tmp_path, archive_handler):
    # Create file with no Date line
    test_file = tmp_path / "no_date.txt"
    test_file.write_text("Content with no date line")
    
    domain, date = archive_handler._extract_newsgroups(str(test_file))
    assert domain == "groups.google.com"
    assert date is None

def test_extract_newsgroups_read_error(archive_handler, monkeypatch):
    # Mock open to raise an exception
    def mock_open(*args, **kwargs):
        raise IOError("Simulated read error")
    
    monkeypatch.setattr("builtins.open", mock_open)
    
    domain, date = archive_handler._extract_newsgroups("any_file.txt")
    assert domain == "groups.google.com"
    assert date is None

# Exception handling tests for _convert_to_archive_url method
def test_convert_to_archive_url_exception(archive_handler, monkeypatch):
    # Force the method to raise an exception
    def mock_extract_domain_and_date(*args, **kwargs):
        raise Exception("Simulated extraction error")
    
    monkeypatch.setattr(archive_handler, "_extract_domain_and_date", mock_extract_domain_and_date)
    
    url = archive_handler._convert_to_archive_url("some/path")
    assert url == ""

def test_convert_to_archive_url_unsupported_type(archive_handler):
    # Test with an unsupported path type that doesn't match websites, mailing-lists or newsgroups
    url = archive_handler._convert_to_archive_url("unsupported/path/type")
    assert url == ""
def test_get_mailing_list_date(archive_handler, monkeypatch):
    # Mock _extract_mailing_lists to return a predictable date
    def mock_extract_mailing_lists(path):
        return "groups.yahoo.com", "2023-01-01T00:00:00+00:00"
    
    monkeypatch.setattr(archive_handler, "_extract_mailing_lists", mock_extract_mailing_lists)
    
    date = archive_handler.get_mailing_list_date("mock/path.json")
    assert date == "2023-01-01T00:00:00+00:00"
    
def test_get_mailing_list_date_none(archive_handler, monkeypatch):
    # Mock _extract_mailing_lists to return None for the date
    def mock_extract_mailing_lists(path):
        return "groups.yahoo.com", None
    
    monkeypatch.setattr(archive_handler, "_extract_mailing_lists", mock_extract_mailing_lists)
    
    date = archive_handler.get_mailing_list_date("mock/path.json")
    assert date is None

def test_extract_mailing_lists_with_date(archive_handler, tmp_path):
    # Create a temporary JSON file with a valid 'date' field
    json_content = {
        "ygData": {
            "date": "1609459200"  # 2021-01-01 00:00:00 UTC
        }
    }
    test_file = tmp_path / "with_date.json"
    test_file.write_text(json.dumps(json_content))
    
    domain, date = archive_handler._extract_mailing_lists(str(test_file))
    assert domain == "groups.yahoo.com"
    assert "2021-01-01" in date  # Check that the date was converted correctly

def test_extract_mailing_lists_with_post_date(archive_handler, tmp_path):
    # Create a temporary JSON file with only a valid 'postDate' field
    json_content = {
        "ygData": {
            "date": None,
            "postDate": "1609545600"  # 2021-01-02 00:00:00 UTC
        }
    }
    test_file = tmp_path / "with_post_date.json"
    test_file.write_text(json.dumps(json_content))
    
    domain, date = archive_handler._extract_mailing_lists(str(test_file))
    assert domain == "groups.yahoo.com"
    assert "2021-01-02" in date  # Check that the date was converted correctly

def test_extract_mailing_lists_both_dates(archive_handler, tmp_path):
    # Create a temporary JSON file with both 'date' and 'postDate' fields
    # 'date' should take precedence
    json_content = {
        "ygData": {
            "date": "1609459200",  # 2021-01-01 00:00:00 UTC
            "postDate": "1609545600"  # 2021-01-02 00:00:00 UTC
        }
    }
    test_file = tmp_path / "both_dates.json"
    test_file.write_text(json.dumps(json_content))
    
    domain, date = archive_handler._extract_mailing_lists(str(test_file))
    assert domain == "groups.yahoo.com"
    assert "2021-01-01" in date  # Should use 'date' field, not 'postDate'

def test_extract_mailing_lists_no_date(archive_handler, tmp_path):
    # Create a temporary JSON file with no valid date fields
    json_content = {
        "ygData": {
            "date": None,
            "postDate": None
        }
    }
    test_file = tmp_path / "no_date.json"
    test_file.write_text(json.dumps(json_content))
    
    domain, date = archive_handler._extract_mailing_lists(str(test_file))
    assert domain == "groups.yahoo.com"
    assert date is None

def test_extract_mailing_lists_empty_date(archive_handler, tmp_path):
    # Create a temporary JSON file with empty date strings
    json_content = {
        "ygData": {
            "date": "",
            "postDate": ""
        }
    }
    test_file = tmp_path / "empty_date.json"
    test_file.write_text(json.dumps(json_content))
    
    domain, date = archive_handler._extract_mailing_lists(str(test_file))
    assert domain == "groups.yahoo.com"
    assert date is None

def test_extract_mailing_lists_zero_date(archive_handler, tmp_path):
    # Create a temporary JSON file with "0" as date value
    json_content = {
        "ygData": {
            "date": "0",
            "postDate": "0"
        }
    }
    test_file = tmp_path / "zero_date.json"
    test_file.write_text(json.dumps(json_content))
    
    domain, date = archive_handler._extract_mailing_lists(str(test_file))
    assert domain == "groups.yahoo.com"
    assert date is None

def test_extract_mailing_lists_file_not_found(archive_handler):
    # Test with a non-existent file path
    with pytest.raises(FileNotFoundError):
        archive_handler._extract_mailing_lists("non_existent_file.json")

def test_extract_mailing_lists_invalid_json(archive_handler, tmp_path):
    # Create a temporary file with invalid JSON content
    test_file = tmp_path / "invalid.json"
    test_file.write_text("This is not valid JSON")
    
    with pytest.raises(json.JSONDecodeError):
        archive_handler._extract_mailing_lists(str(test_file))

def test_extract_mailing_lists_missing_ygdata(archive_handler, tmp_path):
    # Create a temporary JSON file missing the ygData field
    json_content = {"someOtherField": "value"}
    test_file = tmp_path / "missing_ygdata.json"
    test_file.write_text(json.dumps(json_content))
    
    with pytest.raises(KeyError):
        archive_handler._extract_mailing_lists(str(test_file))
