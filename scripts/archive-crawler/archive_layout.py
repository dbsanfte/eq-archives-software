"""Exact Linux Wayback downloader paths, shared by operator and portal captures."""

from urllib.parse import unquote_plus, urlsplit

from common import CrawlError, original_url, tier


def archive_path(capture):
    # Match the public downloader's Linux all-timestamps convention, including
    # CGI::unescape and extensionless-page directory/index.html handling. Keep
    # exact original URL identity in the manifest; refuse ambiguous collisions.
    url = capture["url"]
    canonical = original_url(url)
    if not canonical or not tier(capture["timestamp"]):
        raise CrawlError("Invalid source URL or timestamp for archive destination")
    try:
        path = unquote_plus(url.split("/", 3)[3] if len(url.split("/", 3)) == 4 else "", errors="strict")
    except UnicodeError:
        raise CrawlError("URL has an invalid encoded archive filename") from None
    if (any(ord(c) < 32 or ord(c) == 127 for c in path) or "\\" in path or path.startswith("/")
            or any(piece in (".", "..") or len(piece.encode()) > 255 for piece in path.split("/"))):
        raise CrawlError("URL cannot be safely represented in the existing archive layout")
    if not path or url.endswith("/") or "." not in path.rstrip("/").split("/")[-1]:
        path = path.rstrip("/") + ("/" if path else "") + "index.html"
    return f"websites/{urlsplit(canonical).netloc}/{capture['timestamp']}/{path}"
