import os
import re
import logging

class ArchiveHandler:
    def __init__(self, newsgroups_path: str=None, mailing_lists_path: str=None, 
                 websites_path: str=None, logger: logging.Logger=None):
        self._newsgroups_path = newsgroups_path or os.environ.get("NEWSGROUPS_PATH", "newsgroups/")
        self._mailing_lists_path = mailing_lists_path or os.environ.get("MAILING_LISTS_PATH", "mailing-lists/")
        self._websites_path = websites_path or os.environ.get("WEBSITES_PATH", "websites/")
        self._logger = logger or logging.getLogger(__name__)

    def _strip_index_html_from_url(self, url: str) -> str:
        if "/index.html" in url:
            return re.sub(r'/index\.html$', '', url)
        return url

    def _convert_to_archive_url(self, relative_path: str) -> str:
        try:
            relative_path = relative_path.replace(os.sep, '/')
            if self._websites_path in relative_path:
                self._logger.debug(f"Converting website file path to archive URL: {relative_path}")
                # We will be passed a file path like this:
                # websites/eq.castersrealm.com/20000612004545/cgi-bin/eq/postings.cgi
                #
                # We need to convert it to an archive URL like this:
                # https://web.archive.org/web/20000612004545/http://eq.castersrealm.com/cgi-bin/eq/postings.cgi
                #
                # First we'll split on self.websites_path to get the domain and date parts, 
                # everything after that will be the path. 
                # 
                pieces = relative_path.split(self._websites_path)
                pieces_rest = pieces[1].split('/')
                domain_piece = pieces_rest[0]
                date_piece = pieces_rest[1]
                path = "/".join(pieces_rest[2:])
                final_url = f"https://web.archive.org/web/{date_piece}/http://{domain_piece}/{path}"
                self._logger.debug(f"Converted website file path to archive URL: {final_url}")
                return final_url
            elif self._mailing_lists_path in relative_path:
                self._logger.debug(f"Converting mailing list file path to archive URL: {relative_path}")
                pieces = relative_path.split(self._mailing_lists_path)
                chop_pieces = pieces[1].split('/')
                chop_listname = chop_pieces[0]
                chop_filename = chop_pieces[1].replace(".json", ".html")
                final_url = f"https://dbsanfte.github.io/eq-archives/mailing-lists/{chop_listname}/html/{chop_filename}"
                self._logger.debug(f"Converted mailing list file path to archive URL: {final_url}")
                return final_url
            elif self._newsgroups_path in relative_path:
                self._logger.debug(f"Converting newsgroup file path to archive URL: {relative_path}")
                pieces = relative_path.split(self._newsgroups_path)
                subfolder_path = pieces[1].removeprefix('/')
                final_url = f"https://dbsanfte.github.io/eq-archives/newsgroups/{subfolder_path}"
                self._logger.debug(f"Converted newsgroup file path to archive URL: {final_url}")
                return final_url
            return ""
        except Exception as e:
            self._logger.error(f"Error converting file path to archive URL: {e}")
            self._logger.exception(e)
            return ""

    def _extract_domain_and_date(self, relative_path: str, full_path: str) -> tuple[str, str]:
        parts = relative_path.split(os.sep)

        if self._is_websites(parts):
            return self._extract_websites(relative_path)
        if self._is_newsgroups(parts):
            return self._extract_newsgroups(full_path)
        if self._is_mailing_lists(parts):
            return self._extract_mailing_lists(full_path)
        
        return None, None

    def _is_websites(self, parts: list[str]) -> bool:
        return len(parts) > 3 and parts[0] == "websites"

    def _is_newsgroups(self, parts: list[str]) -> bool:
        return len(parts) > 0 and parts[0] == "newsgroups"

    def _is_mailing_lists(self, parts: list[str]) -> bool:
        return len(parts) > 0 and parts[0] == "mailing-lists"

    def _extract_websites(self, relative_path: str) -> tuple[str, str]:
        relative_path = relative_path.replace(os.sep, '/')
        # We will be passed a file path like this:
        # websites/eq.castersrealm.com/20000612004545/cgi-bin/eq/postings.cgi
        pieces = relative_path.split(self._websites_path)
        pieces_rest = pieces[1].split('/')
        domain_name = pieces_rest[0]
        date = pieces_rest[1]
        date_formatted = f"{date[:4]}-{date[4:6]}-{date[6:8]} {date[8:10]}:{date[10:12]}:{date[12:14]}"
        return domain_name, date_formatted

    def _extract_newsgroups(self, full_path: str) -> tuple[str, str]:
        domain_name = "groups.google.com"
        capture_date = None
        try:
            with open(full_path, 'r') as file:
                for line in file:
                    if line.startswith('Date: '):
                        capture_date = line[6:].strip()
                        break
            self._logger.debug(f"Extracted newsgroup date: {capture_date}")
        except Exception as e:
            self._logger.error(f"Error extracting newsgroup date from {full_path}: {e}")
            self._logger.exception(e)
        return domain_name, capture_date

    def _extract_mailing_lists(self, full_path: str) -> tuple[str, str]:
        domain_name = "groups.yahoo.com"
        capture_date = None
        try:
            with open(full_path, 'r') as file:
                for line in file:
                    if '<b>Date:</b>' in line:
                        match = re.search(r'<b>Date:</b>\s*(.*?)\s*<br/>', line)
                        if match:
                            capture_date = match.group(1).strip()
                            break
            self._logger.debug(f"Extracted mailing list date: {capture_date}")
        except Exception as e:
            self._logger.error(f"Error extracting mailing list date from {full_path}: {e}")
            self._logger.exception(e)
        return domain_name, capture_date

    def _resolve_thumbnail_url(self, url: str, file_type: str) -> str:
        default = "thumbnails/other.webp"
        default_image = "thumbnails/other-image.webp"
        
        if url is None:
            if file_type == "image":
                return default_image
            return default
        
        if "web.archive.org/" in url:
            if "allakhazam.com/" in url:
                return "thumbnails/allakhazam.webp"
            elif "castersrealm.com/" in url or "crgaming.com/" in url:
                return "thumbnails/castersrealm.webp"
            elif file_type == "image":
                return "thumbnails/website-image.webp"
            elif file_type == "text":
                return "thumbnails/website.webp"
            else:
                return "thumbnails/other.webp"
        
        elif self._mailing_lists_path.replace(os.sep, '/') in url:
            return "thumbnails/mailing-list.webp"
        
        elif self._newsgroups_path.replace(os.sep, '/') in url:
            return "thumbnails/newsgroup.webp"
        
        if file_type == "image":
            return "thumbnails/other-image.webp"
        else:
            return default