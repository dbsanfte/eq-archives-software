"""Literal archive scopes and conservative page/source-verified discussion identities."""
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

VERSION = "forum-discussions-v1"
REPLAY = re.compile(r"https?://web\.archive\.org/web/[0-9]{14}(?:[a-z]+_)?/(https?://.+)", re.I)
CAPTURE = re.compile(r"websites/([^/]+)/[0-9]{14}/(.*)")
SHARD = r"(www\.)?(server|pub|p|b)[0-9]+\.ezboard\.com(:80|:443|_80|_443)?"


def literal(value):
    # Lucene optional operators (@, &, ~, #, <, >) also require escaping.
    return re.sub(r'[.?+*|{}\[\]()"\\#@&<>~]', lambda m: "\\" + m[0], value)


def parsed_url(value):
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        raise ValueError("Use a literal HTTP(S) URL without whitespace")
    value.encode("utf-8")
    replay = REPLAY.fullmatch(value)
    parts = urlsplit(replay[1] if replay else value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.fragment or not re.fullmatch(r"[A-Za-z0-9.-]+", parts.hostname)):
        raise ValueError("Use a literal HTTP(S) URL without credentials or fragment")
    _ = parts.port
    return parts


def scope_parts(scope):
    parts = parsed_url(scope.url)
    path = unquote(parts.path).lstrip("/")
    if any(ord(c) < 32 for c in path + unquote(parts.query)) or any(p in (".", "..") for p in path.split("/")):
        raise ValueError("Unsafe URL path")
    if scope.kind in ("host", "directory", "ezboard_board") and parts.query:
        raise ValueError("Use page scope for a query-string URL")
    if scope.kind == "host" and path:
        raise ValueError("Host scopes must use the site root")
    if scope.kind == "ezboard_board":
        path = re.sub(r"/(?:index\.html?)?$", "", path, flags=re.I)
    if scope.kind == "ezboard_board" and (not re.fullmatch(SHARD, parts.netloc, re.I)
                                          or not re.fullmatch(r"b[a-z0-9_-]{1,100}", path, re.I)):
        raise ValueError("Use a top-level Ezboard board URL, such as http://pub4.ezboard.com/beqasylum")
    return parts, path + ("?" + unquote(parts.query) if parts.query else "")


def site_scope_url(domain):
    # Old filesystem folders encode default ports with underscores. Never turn
    # arbitrary archived metadata into a credential-bearing or unsafe link.
    try:
        domain = re.sub(r"_(80|443)$", r":\1", domain)
        parts = parsed_url("http://" + domain + "/")
        if parts.path != "/" or parts.query:
            return None
        return urlunsplit((parts.scheme, parts.netloc.lower(), "/", "", ""))
    except (TypeError, ValueError):
        return None


def host_pattern(parts):
    host = parts.hostname.lower()
    # Only the customary www alias and default ports share a site boundary.
    if host.startswith("www."):
        host = host[4:]
    suffix = r"(:80|:443|_80|_443)?" if parts.port in (None, 80, 443) else literal(":" + str(parts.port))
    return "(www\\.)?" + literal(host) + suffix


def scope_query(scope, forums=()):
    parts, path = scope_parts(scope)
    host = SHARD if scope.kind == "ezboard_board" else host_pattern(parts)
    if scope.kind == "host":
        suffix = ".*"
    elif scope.kind == "directory":
        suffix = literal(path.rstrip("/")) + "(/.*)?" if path else ".*"
    elif scope.kind == "ezboard_board":
        suffix = "(" + "|".join([literal(path) + r"(/(index\.html?)?)?", *[literal(f) + r"([.?/].*)?" for f in forums]]) + ")"
    else:
        suffix = literal(path)
    return {"regexp": {"id": {"value": "websites/" + host + "/[0-9]{14}/" + suffix,
                               "max_determinized_states": 2000}}}


def board_forums(scope, hits):
    """Board-index outgoing forum links establish membership, never concatenation alone."""
    _, board = scope_parts(scope)
    forums = set()
    for hit in hits:
        source = hit.get("_source", {})
        stored = CAPTURE.fullmatch(source.get("id") or hit.get("_id", ""))
        if (not stored or not re.fullmatch(SHARD, stored[1], re.I)
                or re.sub(r"/(?:index\.html?)?$", "", stored[2], flags=re.I).lower() != board.lower()):
            continue
        text = source.get("text_full", "")
        if not isinstance(text, str):
            continue
        # Markdown destinations and literal source URLs only; never follow them.
        for url in re.findall(r'https?://[^\s<>"\)]+', text):
            try:
                parsed = parsed_url(url)
            except ValueError:
                continue
            token = parsed.path.lstrip("/").split(".", 1)[0]
            if (re.fullmatch(SHARD, parsed.netloc, re.I) and re.fullmatch(r"f[a-z0-9_-]{1,200}", token, re.I)
                    and token.lower().startswith("f" + board[1:].lower()) and len(token) > len(board)):
                forums.add(token)
    if len(forums) > 100:
        raise ValueError("Board scope exceeds the 100-forum evidence bound; use narrower directory scopes")
    return sorted(forums)


def page_identity(hit):
    source = hit.get("_source", {})
    if source.get("parent_id"):
        return None
    match = CAPTURE.fullmatch(source.get("id") or hit.get("_id", ""))
    replay = REPLAY.fullmatch(source.get("url") or "")
    if not match:
        return None
    try:
        parts = parsed_url(replay[1] if replay else "http://" + match[1] + "/" + match[2])
    except ValueError:
        return None
    return "page:" + urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, "")), match[1], match[2]


def discussion_identity(hit):
    page = page_identity(hit)
    proof = hit.get("_source", {}).get("discussion")
    if not page or not isinstance(proof, dict) or proof.get("version") != VERSION:
        return None
    if (proof.get("platform") not in ("lithium", "phpbb", "vbulletin", "snitz", "ezboard")
            or any(not isinstance(proof.get(k), str) or not re.fullmatch(r"[a-f0-9]{64}", proof[k])
                   for k in ("key", "source_sha256", "evidence_sha256"))
            or not re.fullmatch(r"[1-9][0-9]{0,19}", str(proof.get("topic", "")))
            or not isinstance(proof.get("forum"), str) or not proof["forum"]
            or not isinstance(proof.get("title"), str) or not proof["title"].strip()
            or not isinstance(proof.get("evidence_id"), str) or not proof["evidence_id"]):
        return None
    origin = urlsplit(page[0][5:])
    if (origin.netloc.lower() != page[1].lower()
            or unquote(origin.path.lstrip("/") + ("?" + origin.query if origin.query else "")) != page[2]):
        return None
    if urlunsplit((origin.scheme, origin.netloc, "", "", "")) != proof.get("origin"):
        return None
    expected = hashlib.sha256(json.dumps([proof[k] for k in ("platform", "origin", "forum", "topic")],
                                          separators=(",", ":")).encode()).hexdigest()
    return "discussion:" + proof["key"] if expected == proof["key"] else None


def group_identity(hit, group_by):
    discussion = discussion_identity(hit) if group_by == "discussion" else None
    page = page_identity(hit)
    return discussion or (page[0] if page else "record:" + hit["_id"])
