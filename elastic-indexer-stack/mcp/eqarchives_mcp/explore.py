"""Cached public archive exploration. Read-only, bounded, and never calls a model."""

import asyncio
from collections import Counter, OrderedDict
from datetime import date, datetime, timezone
import hashlib
import math
import re
import time
from urllib.parse import urlsplit, urlunsplit

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


SAMPLE_SIZE = 100
EXCERPT_CHARS = 12000
CACHE_SECONDS = 600
CACHE_ENTRIES = 64
MAX_VOCABULARY = 50000
STOP = set("""a about above after again against all also am an and any are as at be because been
before being below between both but by can could did do does doing down during each few for
from further get got had has have having he her here hers herself him himself his how i if in
into is it its itself just like me more most much my myself no nor not now of off on once only
or other our ours ourselves out over own same she should so some such than that the their theirs
them themselves then there these they this those through to too under until up us very was we
were what when where which while who whom why will with would you your yours yourself yourselves
http https www com html htm asp php jpg gif png nbsp page pages home homepage welcome click
link links next previous back top menu navigation copyright reserved rights privacy policy
login logout register registration password username email search contact sitemap print printable
javascript browser cookies powered reply replies author posted posts post message messages topic
topics thread threads board boards forum forums index last first new read view go return online
offline joined member members admin administrator moderator quote edit delete subscribe account
profile user users updated update pm am mon tue wed thu fri sat sun jan feb mar apr jun jul aug sep
oct nov dec january february march april june july august september october november december
navigation advertisement advertisements advertising loading continued content table contents
url image images img registered site sites name names list lists time times inc ezboard jump add
info information start total see use using used said says say httpurl htmlurl wwwurl today yesterday
anyone someone make made want way things thing number date free filter settings displayed setting
one two three may must well even still really many every something anything don't doesn't didn't
isn't can't won't i'm i've you're it's that's there's please thanks thank faq help poweredby
everquest eq sony verant entertainment allakhazam castersrealm alla zam google yahoo
comment comments e-mail glossary stats subject submitted know best good great
""".split())
TOKEN = re.compile(r"(?<!\w)[a-z]+(?:['’-][a-z]+)*(?!\w)", re.I)
REPLAY = re.compile(r"^https?://web\.archive\.org/web/\d{14}(?:[a-z]+_)?/(https?://.+)$", re.I)
ARCHIVE_ID = re.compile(r"^websites/([^/]+)/\d{14}/(.*)$")


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date = date(1999, 1, 1)
    end: date = date(2006, 12, 31)
    basis: Literal["capture_date", "llm_guessed_date"] = "capture_date"
    theme: str = Field(default="", max_length=80, pattern=r"^[^\x00-\x1f]*$")
    site: str = Field(default="", max_length=255, pattern=r"^[^\x00-\x1f]*$")
    phrase: str = Field(default="", max_length=100, pattern=r"^[^\x00-\x1f]*$")

    @model_validator(mode="after")
    def dates(self):
        if self.start > self.end or self.start.year < 1990 or self.end.year > 2099 or self.end.year - self.start.year > 49:
            raise ValueError("Choose an ordered date range of at most 50 years between 1990 and 2099.")
        return self

    def query(self):
        filters = [{"prefix": {"id": "websites/"}}, {"range": {self.basis: {
            "gte": str(self.start) + "T00:00:00.000Z", "lte": str(self.end) + "T23:59:59.999Z",
        }}}]
        for field, value in (("llm_tags", self.theme), ("domain_name", self.site)):
            if value:
                filters.append({"term": {field: value}})
        if self.phrase:
            filters.append({"match_phrase": {"text_full": self.phrase}})
        return {"bool": {"filter": filters}}


def overview_query(selection):
    return {"size": 0, "_source": False, "track_total_hits": True, "timeout": "8s",
        "query": selection.query(), "aggs": {
            "sites_count": {"cardinality": {"field": "domain_name", "precision_threshold": 3000}},
            "tagged": {"filter": {"exists": {"field": "llm_tags"}}},
            "themes": {"terms": {"field": "llm_tags", "size": 24, "show_term_doc_count_error": True}},
            "sites": {"terms": {"field": "domain_name", "size": 20, "show_term_doc_count_error": True, "exclude": ""}},
            "timeline": {"date_histogram": {"field": selection.basis, "calendar_interval": "year",
                "format": "yyyy", "min_doc_count": 0,
                "extended_bounds": {"min": str(selection.start.year), "max": str(selection.end.year)}},
                "aggs": {"sites": {"cardinality": {"field": "domain_name", "precision_threshold": 1000}}}},
        }}


def phrases_query(selection):
    query = selection.query()
    query["bool"]["filter"].append({"exists": {"field": "text_full"}})
    return {"size": 0, "_source": False, "track_total_hits": False, "timeout": "8s",
        "query": {"function_score": {"query": query,
            "random_score": {"seed": 2718, "field": "_seq_no"}, "boost_mode": "replace"}},
        "aggs": {"sample": {"diversified_sampler": {"field": "domain_name",
            "shard_size": SAMPLE_SIZE, "max_docs_per_value": SAMPLE_SIZE if selection.site else 8},
            "aggs": {"pages": {"top_hits": {"size": SAMPLE_SIZE,
                "_source": ["id", "url", "domain_name", "parent_id"],
                # Fetch-stage only: no fielddata or source-sized responses. Text is
                # clipped before transport and never sent to the browser.
                "script_fields": {"excerpt": {"script": {"lang": "painless", "source":
                    "def t = params._source.text_full; if (!(t instanceof String)) return ''; "
                    "int limit = params.limit; return t.length() > limit ? t.substring(0, limit) : t;",
                    "params": {"limit": EXCERPT_CHARS}}}},
            }}}}}}


def page_identity(source, document_id):
    """Same conservative URL identity as the search reader; do not merge accounts."""
    if source.get("parent_id"):
        return document_id
    replay = REPLAY.match(source.get("url", ""))
    path = ARCHIVE_ID.match(source.get("id") or document_id)
    raw = replay[1] if replay else f"http://{path[1]}/{path[2]}" if path else ""
    try:
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return document_id
        port = parts.port
        host = parts.hostname.lower()
        if port and (parts.scheme, port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    except ValueError:
        return document_id


def extract_phrases(hits, site_selected=False):
    pages, seen_pages, seen_text, domains = [], set(), set(), Counter()
    clipped = duplicates = 0
    for hit in hits[:SAMPLE_SIZE]:
        source = hit["_source"]
        text = hit["fields"]["excerpt"][0]
        if not isinstance(text, str) or not text.strip():
            continue
        identity = page_identity(source, hit["_id"])
        text = text[:EXCERPT_CHARS]
        fingerprint = hashlib.sha256(" ".join(text.split()).encode()).digest()
        if identity in seen_pages or fingerprint in seen_text:
            duplicates += 1
            continue
        domain = source.get("domain_name", "")
        if not site_selected and domains[domain] >= 8:
            continue
        seen_pages.add(identity)
        seen_text.add(fingerprint)
        domains[domain] += 1
        clipped += len(text) == EXCERPT_CHARS
        # text_full contains Markdown. Link destinations (including relative
        # archive paths) are not source prose. Keep boundaries when removing
        # markup, URLs and entities so phrases still occur in the indexed text.
        text = text.lower()
        text = re.sub(r"(?m)^[ \t]{0,3}\[[^\]\n]+\]:[^\n]*", "\n", text)
        text = re.sub(r"(?<=\])\([^\n]*?\)", "\n", text)
        text = re.sub(r"<[^>]*>|&(?:\#\d+|\#x[0-9a-f]+|[a-z]+);", "\n", text)
        text = re.sub(r"(?:https?://|www\.)\S+|\b\S+@\S+\b", "\n", text)
        # Retain sentence boundaries: never invent a phrase across unrelated lines.
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        pages.append((domain, lines))

    repeated = Counter((domain, line) for domain, lines in pages for line in set(lines))
    global_lines = Counter(line for _, lines in pages for line in set(lines))
    frequency = Counter()
    domain_frequency = {domain: Counter() for domain in domains}
    vocabulary_limited = False
    for domain, lines in pages:
        phrases = set()
        for line in lines:
            if (domains[domain] >= 3 and repeated[domain, line] >= max(3, math.ceil(domains[domain] * .6))) or (len(line) < 200 and global_lines[line] >= max(5, math.ceil(len(pages) * .05))):
                continue
            for sentence in re.split(r"[.!?;|\[\]{}<>]+", line):
                matches = list(TOKEN.finditer(sentence))
                tokens = [match[0] for match in matches]
                for start, token in enumerate(tokens):
                    if token in STOP or not 3 <= len(token) <= 24:
                        continue
                    phrases.add(token)
                    for length in (2, 3):
                        words = tokens[start:start + length]
                        adjacent = all(sentence[a.end():b.start()].isspace()
                            for a, b in zip(matches[start:start + length - 1], matches[start + 1:start + length]))
                        if len(words) == length and adjacent and all(3 <= len(w) <= 24 and w not in STOP for w in words):
                            phrases.add(" ".join(words))
        for phrase in sorted(phrases):
            if phrase in frequency or len(frequency) < MAX_VOCABULARY:
                frequency[phrase] += 1
                domain_frequency[domain][phrase] += 1
            else:
                vocabulary_limited = True
    minimum = 2 if len(pages) >= 8 else 1
    # Repeated field labels can survive line removal when each row also contains
    # a changing item ID/value. Suppress words shared by almost the entire sample,
    # including phrases made from those labels, while retaining true page counts.
    ubiquitous = {p for p, n in frequency.items()
        if ' ' not in p and len(pages) >= 8 and n >= math.ceil(len(pages) * .8)}
    # Inline navigation changes as a whole when any ID/value changes. Detect
    # repeated phrases within those rows, using the same per-domain threshold
    # as repeated lines. Require most support to come from such template repeats,
    # so a topic discussed independently across sources remains eligible.
    template_frequency = Counter()
    for domain, counts in domain_frequency.items():
        threshold = max(3, math.ceil(domains[domain] * .6))
        for phrase, n in counts.items():
            if ' ' in phrase and n >= threshold:
                template_frequency[phrase] += n
    ranked = sorted((p for p, n in frequency.items()
        if n >= minimum and not ubiquitous.intersection(p.split()) and template_frequency[p] < n * .8),
        key=lambda p: (-frequency[p] * (1 + p.count(" ")), p))
    chosen = []
    for phrase in ranked:
        if any(f" {phrase} " in f" {other} " and frequency[phrase] == frequency[other] for other in chosen):
            continue
        chosen.append(phrase)
        if len(chosen) == 40:
            break
    return {"phrases": [{"text": p, "pages": frequency[p]} for p in chosen],
        "sampled_pages": len(pages), "sampled_sites": len(domains), "examined_captures": len(hits),
        "duplicate_captures": duplicates, "clipped_pages": clipped, "excerpt_chars": EXCERPT_CHARS,
        "sample_limit": SAMPLE_SIZE, "vocabulary_limited": vocabulary_limited}


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid count")
    return value


def parse_overview(data):
    aggs = data["aggregations"]
    total = data["hits"]["total"]
    if total["relation"] != "eq":
        raise ValueError("Incomplete total")
    result = {"records": count(total["value"]), "sites_count": count(aggs["sites_count"]["value"]),
        "tagged": count(aggs["tagged"]["doc_count"])}
    for name in ("themes", "sites"):
        result[name] = []
        for bucket in aggs[name]["buckets"]:
            if not isinstance(bucket["key"], str) or not bucket["key"]:
                continue
            result[name].append({"key": bucket["key"], "count": count(bucket["doc_count"]),
                "approximate": bucket.get("doc_count_error_upper_bound", -1) != 0})
    result["timeline"] = [{"year": int(b["key_as_string"]), "records": count(b["doc_count"]),
        "sites": count(b["sites"]["value"])} for b in aggs["timeline"]["buckets"]]
    return result


class Explorer:
    def __init__(self, archive):
        self.archive = archive
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    async def get(self, kind, selection):
        key = (kind, selection.model_dump_json())
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return cached[1]
        # A single analytics query at a time, with a bounded wait for simultaneous
        # overview/cloud requests. No background scans or competing reindex jobs.
        try:
            async with asyncio.timeout(22):
                async with self.lock:
                    cached = self.cache.get(key)
                    if cached and cached[0] > time.monotonic():
                        return cached[1]
                    if self.archive.requests.locked():
                        raise ToolError("Archive exploration is busy. Please retry shortly.")
                    async with self.archive.requests:
                        data = await self.archive.execute(overview_query(selection) if kind == "overview" else phrases_query(selection))
                    if kind == "overview":
                        result = parse_overview(data)
                    else:
                        hits = data["aggregations"]["sample"]["pages"]["hits"]["hits"]
                        result = await asyncio.to_thread(extract_phrases, hits, bool(selection.site))
                    result["generated_at"] = datetime.now(timezone.utc).isoformat()
                    result["selection"] = selection.model_dump(mode="json")
                    self.cache[key] = (time.monotonic() + CACHE_SECONDS, result)
                    self.cache.move_to_end(key)
                    while len(self.cache) > CACHE_ENTRIES:
                        self.cache.popitem(last=False)
                    return result
        except (TimeoutError, KeyError, TypeError, ValueError, IndexError, AttributeError):
            raise ToolError("Archive exploration is temporarily unavailable. Please retry.") from None
