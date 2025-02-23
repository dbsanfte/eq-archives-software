import os
import re
import logging

logger = logging.getLogger(__name__)

class ArchiveHandler:
    def __init__(self):
        self.newsgroups_path = os.environ.get("NEWSGROUPS_PATH", "newsgroups/".replace("/", os.sep))
        self.mailing_lists_path = os.environ.get("MAILING_LISTS_PATH", "mailing-lists/".replace("/", os.sep))
        self.websites_path = os.environ.get("WEBSITES_PATH", "websites/".replace("/", os.sep))

    def _strip_index_html_from_url(self, url: str) -> str:
        if "/index.html" in url:
            return re.sub(r'/index\.html$', '', url)
        return url

    def _convert_to_archive_url(self, file_path: str) -> str:
        try:
            if self.websites_path in file_path:
                pieces = file_path.split(self.websites_path)
                chop = pieces[1]
                chop_pieces = chop.split(os.sep)
                chop_domain = chop_pieces[1]
                chop_date = chop_pieces[2]
                chop_path_pieces = chop.split(f"{os.sep}{chop_date}{os.sep}")
                if len(chop_path_pieces) < 2:
                    return file_path
                chop_path = chop_path_pieces[1]
                final_url = f"https://web.archive.org/web/{chop_date}/http://{chop_domain}/{chop_path}"
                return final_url
            elif self.mailing_lists_path in file_path:
                pieces = file_path.split(self.mailing_lists_path)
                chop_pieces = pieces[1].split(os.sep)
                chop_listname = chop_pieces[1]
                chop_filename = chop_pieces[2].replace(".json", ".html")
                return f"https://dbsanfte.github.io/eq-archives/mailing-lists/{chop_listname}/html/{chop_filename}"
            elif self.newsgroups_path in file_path:
                pieces = file_path.split(self.newsgroups_path)
                subfolder_path = pieces[1].removeprefix(os.sep).replace(os.sep, "/")
                return f"https://dbsanfte.github.io/eq-archives/newsgroups/{subfolder_path}"
            return ""
        except Exception as e:
            logger.error(f"Error converting file path to archive URL: {e}")
            logger.exception(e)
            return ""

    def _extract_domain_and_date(self, relative_path: str, full_path: str) -> tuple[str, str]:
        parts = relative_path.split(os.sep)

        if self._is_websites(parts):
            return self._extract_websites(parts)
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

    def _extract_websites(self, parts: list[str]) -> tuple[str, str]:
        return parts[1], parts[2]

    def _extract_newsgroups(self, full_path: str) -> tuple[str, str]:
        domain_name = "groups.google.com"
        capture_date = None
        try:
            with open(full_path, 'r') as file:
                for line in file:
                    if line.startswith('Date: '):
                        capture_date = line[6:].strip()
                        break
            logger.debug(f"Extracted newsgroup date: {capture_date}")
        except Exception as e:
            logger.error(f"Error extracting newsgroup date from {full_path}: {e}")
            logger.exception(e)
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
            logger.debug(f"Extracted mailing list date: {capture_date}")
        except Exception as e:
            logger.error(f"Error extracting mailing list date from {full_path}: {e}")
            logger.exception(e)
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
        
        elif self.mailing_lists_path.replace(os.sep, '/') in url:
            return "thumbnails/mailing-list.webp"
        
        elif self.newsgroups_path.replace(os.sep, '/') in url:
            return "thumbnails/newsgroup.webp"
        
        if file_type == "image":
            return "thumbnails/other-image.webp"
        else:
            return default