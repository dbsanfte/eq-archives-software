"""Private, resumable crawl state. No archive writes or indexing credentials."""

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

TIERS = {1: ("19990101000000", "20011231235959"),
         2: ("20020101000000", "20071231235959")}
CATEGORIES = ["guild", "news", "aggregator", "personal_blog", "forum", "class",
              "independent_information", "other", "unrelated"]
SHARED_HOSTS = ("geocities.com", "angelfire.com", "members.aol.com", "home.att.net",
                "home.earthlink.net", "members.tripod.com", "members.tripod.co.uk", "ezboard.com",
                "homes.arealcity.com", "go.to", "tyler.net")


class CrawlError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def tier(timestamp):
    if not re.fullmatch(r"\d{14}", str(timestamp)):
        return None
    try:
        datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return next((n for n, (start, end) in TIERS.items() if start <= timestamp <= end), None)


def original_url(value, base=None):
    """Preserve protocol, path case, escaping and query order; remove fragments."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if base:
        value = urljoin(base, value)
    replay = re.match(r"^https?://web\.archive\.org/web/\d{1,14}(?:[a-z]+_)?/(.+)$", value, re.I)
    if replay:
        value = replay[1]
        if not value.startswith(("http://", "https://")):
            value = "http://" + value
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password
                or any(ord(c) < 32 or c.isspace() for c in value) or "\\" in value):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if host.lower() == "localhost" or host.lower().endswith((".local", ".internal")):
                return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
        netloc = parsed.netloc.lower()
        if parsed.port == (80 if parsed.scheme == "http" else 443):
            netloc = netloc.rsplit(":", 1)[0]
        return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def site_scope(url):
    parsed = urlsplit(url)
    pieces = [p for p in parsed.path.strip("/").split("/") if p]
    host = parsed.hostname.lower()
    # These shared hosts contain unrelated sites. Bound approvals to an account.
    shared = SHARED_HOSTS
    name = host.removeprefix("www.")
    count = 0
    if pieces and pieces[0].startswith("~"):
        count = 1
    if name in shared or host.endswith(".ezboard.com"):
        count = 1
        if name == "angelfire.com":
            count = 2
        if name == "geocities.com":
            # Neighborhood addresses have a numeric account component. Later
            # account names live directly under /username/.
            address = next((i for i, piece in enumerate(pieces[:3]) if piece.isdigit()), None)
            count = address + 1 if address is not None else 1
    account = pieces[:count]
    if host.endswith(".ezboard.com") and account:
        if re.fullmatch(r"b[^/]+", account[0], re.I):
            account[0] = re.sub(r"\.html?$", "", account[0], flags=re.I)
        else:
            return url  # Forum/thread filenames do not reliably identify a board.
    if account and re.search(r"\.(?:html?|shtml|php|asp)$", account[-1], re.I):
        account.pop()  # A page filename cannot be an account directory.
    if name == "sitepowerup.com" or (name in shared and not account):
        return url  # Unidentified shared-host accounts require exact-page scope.
    path = "/" + "/".join(account) + "/" if account else "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path.replace("//", "/"), "", ""))


def within_scope(url, scope):
    url = original_url(url)
    if not url:
        return False
    page, boundary = urlsplit(url), urlsplit(scope)
    if (page.scheme, page.netloc) != (boundary.scheme, boundary.netloc):
        return False
    # A shared-host root does not identify an account. Even a trailing slash
    # must remain exact rather than granting traversal of every hosted site.
    host = boundary.hostname.lower().removeprefix("www.")
    if host == "sitepowerup.com" or boundary.path == "/" and (host in SHARED_HOSTS or host.endswith(".ezboard.com")):
        return url == scope
    if boundary.query or not boundary.path.endswith("/"):
        return url == scope
    if boundary.hostname.endswith(".ezboard.com") and page.path in (boundary.path.rstrip("/") + ".html", boundary.path.rstrip("/") + ".htm"):
        return True
    return page.path == boundary.path.rstrip("/") or page.path.startswith(boundary.path)


def site_identity(url):
    """Discovery identity only; source URL and document identity stay exact."""
    parsed = urlsplit(site_scope(original_url(url)))
    return (parsed.netloc.removeprefix("www."), parsed.path, parsed.query)


def capture_scope(url, mode="directory", path=None):
    owner = site_scope(url)
    if mode == "custom":
        if (not isinstance(path, str) or not 1 <= len(path) <= 2048 or not path.startswith("/")
                or path.startswith("//") or any(character in path for character in "?#*\\")
                or any(character.isspace() or ord(character) < 32 for character in path)):
            raise CrawlError("Enter an absolute website folder path, such as /eq/research/")
        try:
            decoded = unquote(path, errors="strict")
        except UnicodeDecodeError:
            raise CrawlError("Custom scope must use valid UTF-8 URL encoding") from None
        if (any(piece in (".", "..") for piece in decoded.split("/")) or "\\" in decoded
                or any(ord(character) < 32 for character in decoded) or re.search(r"%2f|%5c|%25", path, re.I)):
            raise CrawlError("Custom scope contains an unsafe or ambiguous folder path")
        parsed = urlsplit(url)
        folder = original_url(urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/") + "/", "", "")))
        if not folder or not within_scope(folder, owner):
            raise CrawlError("Custom folder must remain within this website or shared-host account")
        return folder
    if mode == "site":
        return owner
    if mode == "page":
        return url
    if mode != "directory":
        raise CrawlError("Choose page, directory or site capture scope")
    parsed = urlsplit(url)
    path = (parsed.path.rstrip("/") + "/" if parsed.path.endswith("/") or "." not in parsed.path.rsplit("/", 1)[-1]
            else parsed.path.rsplit("/", 1)[0] + "/")
    directory = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return directory if within_scope(directory, owner) else owner


class Page(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.links, self.text, self.title = base, [], [], []
        self.anchor, self.ignored, self.in_title = None, 0, False

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or "" for key, value in attrs}
        if tag in ("script", "style"):
            self.ignored += 1
        if tag == "title":
            self.in_title = True
        if self.ignored:
            return
        if tag == "base" and attrs.get("href"):
            self.base = original_url(attrs["href"], self.base) or self.base
        if tag in ("a", "area", "frame", "iframe"):
            link = original_url(attrs.get("href", attrs.get("src", "")), self.base)
            if link and len(self.links) < 3000:
                row = {"url": link, "anchor": attrs.get("alt", ""),
                       "context": " ".join(self.text[-12:])[-600:]}
                self.links.append(row)
                if tag == "a":
                    self.anchor = row
        if tag == "img" and self.anchor:
            self.anchor["anchor"] += " " + attrs.get("alt", "")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.ignored:
            self.ignored -= 1
        if tag == "a":
            self.anchor = None
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.ignored:
            return
        data = " ".join(data.split())
        if not data:
            return
        self.text.append(data)
        if self.in_title:
            self.title.append(data)
        if self.anchor:
            self.anchor["anchor"] = (self.anchor["anchor"] + " " + data)[:600]


def decode(data, content_type=""):
    match = re.search(r"charset=[\"']?([\w-]+)", content_type, re.I)
    encodings = ([match[1]] if match else []) + ["utf-8", "windows-1252", "latin-1"]
    for encoding in encodings:
        try:
            return data.decode(encoding), encoding
        except (UnicodeError, LookupError):
            continue
    raise CrawlError("Could not decode source")


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


class Store:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        repo = Path(__file__).resolve().parents[2]
        if self.root == repo or repo in self.root.parents:
            raise CrawlError("Keep crawl state outside the software repository")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.db = sqlite3.connect(self.root / "crawl.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY, tree TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tree_state(tree TEXT PRIMARY KEY, complete INTEGER NOT NULL, entries INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS files(tree TEXT, path TEXT, blob TEXT, PRIMARY KEY(tree,path));
            CREATE TABLE IF NOT EXISTS scans(blob TEXT, source TEXT, evidence TEXT, PRIMARY KEY(blob,source));
            CREATE TABLE IF NOT EXISTS links(url TEXT, source TEXT, evidence TEXT, PRIMARY KEY(url,source));
            CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, url TEXT UNIQUE, scope TEXT,
                priority INTEGER, coverage TEXT, evidence TEXT, state TEXT DEFAULT 'discovered',
                captures TEXT DEFAULT '[]', rating TEXT, error TEXT, decision TEXT);
            CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, candidate TEXT,
                signature TEXT, reserved REAL, actual REAL, status TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, candidate TEXT, action TEXT, detail TEXT, created TEXT);
        """)
        (self.root / "crawl.sqlite3").chmod(0o600)
        archive = self.get("archive_repository")
        if archive and Path(archive).resolve() in (self.root, *self.root.parents):
            self.db.close()
            raise CrawlError("Keep crawl state outside the archive checkout")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def event(self, candidate, action, detail):
        self.db.execute("INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)",
                        (candidate, action, json.dumps(detail), now()))
        self.db.commit()

    def candidates(self):
        return list(self.db.execute("SELECT * FROM candidates ORDER BY priority DESC,url"))

    def close(self):
        self.db.close()
