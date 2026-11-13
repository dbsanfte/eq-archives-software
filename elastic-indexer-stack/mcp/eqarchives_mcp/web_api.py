"""Bounded browser adapter. Rebuild the supported Search UI grammar, never relay DSL.

The old URLs remain compatible with cached browsers. Indexes, source projections,
highlight markup, budgets and upstream URLs belong to the server. Unknown syntax
fails closed, including scripts, runtime fields, scroll and arbitrary aggregations.
"""
import asyncio
from datetime import datetime
import json
import math
import re
import time

from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import JSONResponse

from .limits import TextBudget
from .http_limits import send_body

FACETS = {"domain_name", "llm_content_flavour", "file_type", "mime_type", "mailing_list_name", "llm_tags"}
DATES = {"capture_date", "llm_guessed_date", "last_indexed", "llm_extracted_dates.date"}
TEXT = ["text_full", "llm_summary", "llm_image_text"]
LIST_FIELDS = ["id", "discussion", "title", "llm_summary", "llm_content_flavour", "llm_tags",
               "capture_date", "llm_guessed_date", "url", "alternate_url", "mailing_list_name",
               "domain_name", "parent_id", "thumbnail", "file_type", "mime_type", "last_indexed"]
READER_FIELDS = ["title", "url", "alternate_url", "text_full", "llm_image_text_full", "capture_date",
                 "llm_guessed_date", "domain_name", "mailing_list_name", "file_type", "mime_type", "parent_id", "discussion"]
HIGHLIGHT = {"encoder": "html", "fields": {"text_full": {
    "fragment_size": 240, "number_of_fragments": 1, "no_match_size": 240}}}
MAX_BODY = 96 * 1024


class WebAdmission:
    """Hold capacity through the response, including a slow/disconnected reader.

    Releasing at query completion would let many slow clients retain complete
    8 MiB responses in memory after bypassing the retrieval semaphore.
    """
    def __init__(self, app):
        self.app = app
        self.slots = asyncio.Semaphore(2)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(("/elasticsearch/", "/openai/")):
            return await self.app(scope, receive, send)
        unavailable = JSONResponse({"error": {"message": "Search is busy. Please retry shortly."}},
                                   status_code=503, headers={"Retry-After": "5", "Cache-Control": "no-store"})
        if self.slots.locked():
            return await unavailable(scope, receive, send)
        started = False
        async def bounded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                await send(message)
            else:
                await send_body(send, message.get("body", b""), message.get("more_body", False))
        async with self.slots:
            try:
                async with asyncio.timeout(30):
                    await self.app(scope, receive, bounded_send)
            except TimeoutError:
                if started:
                    raise  # Close an incomplete transfer; never return truncated JSON as success.
                await unavailable(scope, receive, send)


def check(condition):
    if not condition:
        raise ValueError("Unsupported or excessive request")


def keys(value, allowed):
    check(isinstance(value, dict) and not value.keys() - set(allowed))
    return value


def text(value, limit=255):
    check(isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value))
    value.encode("utf-8")  # JSON escapes can decode to lone surrogates, not valid UTF-8.
    return value


def integer(value, low, high):
    check(type(value) is int and low <= value <= high)
    return value


def items(value, limit=20):
    check(isinstance(value, list) and len(value) <= limit)
    return value


class Builder:
    def __init__(self):
        self.nodes = 0
        self.text_budget = TextBudget()

    def analyzed_text(self, value, limit=1000):
        return self.text_budget.accept(value, limit)

    def query(self, value, depth=0):
        self.nodes += 1
        check(self.nodes <= 160 and depth <= 8 and isinstance(value, dict) and len(value) == 1)
        kind, arg = next(iter(value.items()))
        if kind == "match_all":
            check(arg == {})
            return {kind: {}}
        if kind == "bool":
            keys(arg, ["must", "must_not", "should", "filter", "minimum_should_match"])
            result = {}
            for name, clauses in arg.items():
                if name == "minimum_should_match":
                    result[name] = integer(clauses, 0, 1)
                else:
                    result[name] = [self.query(c, depth + 1) for c in items(clauses)]
            return {kind: result}
        if kind in ("term", "terms"):
            check(isinstance(arg, dict) and len(arg) == 1)
            field, values = next(iter(arg.items()))
            check(field in FACETS | {"embedding_model", "id"})
            return {kind: {field: [text(v) for v in items(values)] if kind == "terms" else text(values, 2048)}}
        if kind == "range":
            check(isinstance(arg, dict) and len(arg) == 1)
            field, bounds = next(iter(arg.items()))
            check(field in DATES)
            keys(bounds, ["gte", "lte", "gt", "lt"])
            result = {}
            for name, date in bounds.items():
                text(date, 40)
                datetime.fromisoformat(date.replace("Z", "+00:00"))
                result[name] = date
            return {kind: {field: result}}
        if kind == "nested":
            keys(arg, ["path", "query"])
            check(arg.get("path") == "llm_extracted_dates")
            return {kind: {"path": "llm_extracted_dates", "query": self.query(arg["query"], depth + 1)}}
        if kind == "exists":
            keys(arg, ["field"])
            check(arg.get("field") in FACETS | DATES | {"id"})
            return {kind: {"field": arg["field"]}}
        if kind == "ids":
            keys(arg, ["values"])
            values = items(arg.get("values"), 1)
            check(len(values) == 1)
            return {kind: {"values": [text(values[0], 2048)]}}
        if kind == "prefix":
            check(arg == {"id": "websites/"})
            return {kind: {"id": "websites/"}}
        if kind == "match_phrase":
            check(isinstance(arg, dict) and set(arg) == {"url"})
            return {kind: {"url": self.analyzed_text(arg["url"], 2048)}}
        if kind == "regexp":
            # Only a literal capture identity separated by a 14-digit timestamp.
            check(isinstance(arg, dict) and set(arg) == {"id"})
            pattern = text(arg["id"], 4096)
            host, separator, path = pattern.removeprefix("websites/").partition("/[0-9]{14}/")
            check(pattern.startswith("websites/") and separator and host and path)
            special = re.compile(r'[.?+*|{}\[\]()"\\#@&<>~]')
            for component in (host, path):
                decoded = re.sub(r"\\(.)", r"\1", component)
                check(special.sub(lambda m: "\\" + m[0], decoded) == component)
            check(re.fullmatch(r"[A-Za-z0-9_.:\\-]+", host) is not None)
            return {kind: {"id": {"value": pattern, "max_determinized_states": 1000}}}
        if kind in ("multi_match", "query_string"):
            keys(arg, ["query", "fields", "type", "operator", "default_operator"])
            query = self.analyzed_text(arg.get("query"))
            check(arg.get("fields") in (TEXT, [f + "^1" for f in TEXT]))
            if kind == "query_string":
                # No field selectors, regexes, wildcard expansion, fuzziness or scripts.
                parts = re.split(r'("(?:\\.|[^"\\])*")', query)
                for i in range(0, len(parts), 2):
                    parts[i] = re.sub(r"\bAND\b", "+", parts[i])
                    parts[i] = re.sub(r"\bOR\b", "|", parts[i])
                    parts[i] = re.sub(r"\bNOT\s+", "-", parts[i])
                query = "".join(parts)
                return {"simple_query_string": {"query": query, "fields": TEXT, "default_operator": "and",
                    "flags": "AND|OR|NOT|PHRASE|PRECEDENCE|WHITESPACE|ESCAPE"}}
            check(arg.get("type") in ("best_fields", "cross_fields", "phrase", "phrase_prefix"))
            check(arg.get("operator", "and") in ("and", "or"))
            result = {"query": query, "fields": TEXT, "type": arg["type"]}
            if "operator" in arg:
                result["operator"] = arg["operator"]
            if arg["type"] == "phrase_prefix":
                result["max_expansions"] = 20
            return {kind: result}
        raise ValueError("Unsupported query")

    def aggs(self, value):
        check(isinstance(value, dict) and len(value) <= 8)
        if set(value) == {"sites"}:
            expected = {"sites": {"terms": {"field": "domain_name", "size": 50,
                "order": [{"latest_indexed": "desc"}, {"_key": "asc"}]}, "aggs": {
                "latest_indexed": {"max": {"field": "last_indexed"}},
                "first_capture": {"min": {"field": "capture_date"}},
                "last_capture": {"max": {"field": "capture_date"}}}}}
            check(value == expected)
            return expected
        result = {}
        buckets = 0
        for name, bucket in value.items():
            check(name in {"facet_bucket_" + f for f in FACETS | {"all", "last_indexed"}})
            keys(bucket, ["filter", "aggs"])
            leaves = keys(bucket.get("aggs"), FACETS | {"last_indexed"})
            children = {}
            for field, leaf in leaves.items():
                if field == "last_indexed":
                    keys(leaf, ["filters"])
                    ranges = keys(leaf.get("filters"), ["filters"])["filters"]
                    keys(ranges, ["Today", "Past week", "Past month", "Past year", "All time"])
                    buckets += len(ranges)
                    children[field] = {"filters": {"filters": {name: self.query(q) for name, q in ranges.items()}}}
                else:
                    keys(leaf, ["terms"])
                    terms = keys(leaf.get("terms"), ["field", "size", "order"])
                    check(terms.get("field") == field and terms.get("order", {"_count": "desc"}) == {"_count": "desc"})
                    children[field] = {"terms": {"field": field, "size": integer(terms.get("size", 100), 1, 100)}}
                    buckets += children[field]["terms"]["size"]
                check(buckets <= 900)
            result[name] = {"filter": self.query(bucket["filter"]), "aggs": children}
        return result

    def search(self, value, model):
        keys(value, ["query", "post_filter", "size", "from", "sort", "_source", "highlight", "aggs", "knn", "timeout", "track_total_hits"])
        size = integer(value.get("size", 20), 0, 100)
        offset = integer(value.get("from", 0), 0, 999)
        size = min(size, 1000 - offset)
        result = {"size": size, "from": offset, "timeout": "8s", "track_total_hits": 1001,
                  "_source": LIST_FIELDS, "query": self.query(value.get("query", {"match_all": {}}))}
        if "ids" in result["query"]:
            check(size == 1 and offset == 0 and not (value.keys() & {"aggs", "knn", "post_filter"}))
            source = value.get("_source", READER_FIELDS)
            # ES interprets an empty includes array as the complete stored source.
            check(isinstance(source, list) and 0 < len(source) <= len(READER_FIELDS)
                  and set(source) <= set(READER_FIELDS))
            result["_source"] = list(dict.fromkeys(source))
        elif value.get("_source") is False:
            result["_source"] = False
        if "post_filter" in value:
            result["post_filter"] = self.query(value["post_filter"])
        if "highlight" in value:
            # Never accept client-selected tags, encoders or source fields.
            result["highlight"] = HIGHLIGHT
        if "sort" in value:
            result["sort"] = []
            for clause in items(value["sort"], 3):
                check(isinstance(clause, dict) and len(clause) == 1)
                field, order = next(iter(clause.items()))
                check(field in DATES | {"_score", "id", "_doc"} and field != "llm_extracted_dates.date")
                if isinstance(order, dict):
                    keys(order, ["order", "missing"])
                    check(order.get("missing", "_last") == "_last")
                    order = order.get("order")
                check(order in ("asc", "desc"))
                result["sort"].append({field: order if field in ("_score", "_doc") else {"order": order, "missing": "_last"}})
        if "aggs" in value:
            result["aggs"] = self.aggs(value["aggs"])
        if "knn" in value:
            branches = value["knn"] if isinstance(value["knn"], list) else [value["knn"]]
            result["knn"] = []
            used = set()
            for branch in items(branches, 4):
                keys(branch, ["field", "query_vector", "k", "num_candidates", "boost", "filter"])
                field = branch.get("field")
                check(field in {"image_vector", "llm_image_text_vector", "llm_summary_vector", "text.vector"} - used)
                used.add(field)
                vector = items(branch.get("query_vector"), 768)
                check(len(vector) == 768 and all(type(x) in (float, int) and abs(x) <= 1 and math.isfinite(x) for x in vector))
                check(0.9 < sum(x*x for x in vector) < 1.1)
                k = integer(branch.get("k"), 1, 50)
                candidates = integer(branch.get("num_candidates"), k, 250)
                filters = branch.get("filter", [])
                filters = filters if isinstance(filters, list) else [filters]
                compiled = [self.query(f) for f in items(filters)]
                compiled.append({"term": {"embedding_model": model}})
                result["knn"].append({"field": field, "query_vector": vector, "k": k,
                    "num_candidates": candidates, "boost": 10, "filter": compiled})
            if result["knn"] and "query" not in value:
                # A default match_all would OR unembedded records into a
                # vector-only request and hide an empty model frontier.
                result.pop("query")
        return result


class WebAPI:
    def __init__(self, archive):
        self.archive = archive
        self.slots = asyncio.Semaphore(2)
        self.count_cache = None

    async def handle(self, request, operation):
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        try:
            if operation == "search":
                check(list(request.query_params.multi_items()) in ([], [("preference", "archive-captures")]))
            else:
                check(not request.url.query)
            raw = bytearray()
            async with asyncio.timeout(2):
                async for part in request.stream():
                    raw.extend(part)
                    check(len(raw) <= MAX_BODY)
            if operation == "count":
                check(not raw)
                if self.count_cache and self.count_cache[0] > time.monotonic():
                    return JSONResponse(self.count_cache[1], headers=headers)
                body = {"size": 0, "_source": False, "track_total_hits": True, "timeout": "8s", "query": {"match_all": {}}}
            else:
                check(request.headers.get("content-type", "").split(";")[0] == "application/json")
                def unique(pairs):
                    check(len(dict(pairs)) == len(pairs))
                    return dict(pairs)
                value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: check(False))
                if operation == "rerank":
                    keys(value, ["query", "ids"])
                    # rank_ids validates the complete fixed contract before any I/O.
                    if self.slots.locked() or self.archive.requests.locked():
                        raise ToolError("Busy")
                    async with self.slots, self.archive.requests, asyncio.timeout(4):
                        ids, status = await self.archive.rank_ids(value.get("query"), value.get("ids"))
                    return JSONResponse({"ids": ids, "status": status}, headers=headers)
                if operation == "embedding":
                    keys(value, ["input", "model", "encoding_format"])
                    check(value.get("model") == self.archive.settings.model and value.get("encoding_format", "float") == "float")
                    inputs = value.get("input")
                    inputs = inputs if isinstance(inputs, list) else [inputs]
                    check(len(inputs) == 1)
                    query = text(inputs[0], 630)
                    prefix = "task: search result | query: "
                    check(query.startswith(prefix))
                    query = text(query[len(prefix):], 600)
                    vector = await self.archive.embedding(query)
                    if vector is None:
                        raise ToolError("Embedding unavailable")
                    return JSONResponse({"object": "list", "model": self.archive.settings.model,
                        "data": [{"object": "embedding", "index": 0, "embedding": vector}]}, headers=headers)
                body = Builder().search(value, self.archive.settings.model)
            if self.slots.locked() or self.archive.requests.locked():
                raise ToolError("Busy")
            async with self.slots, self.archive.requests, asyncio.timeout(10):
                result = await self.archive.execute(body, stable_order=True)
            if operation == "count":
                result = {"count": result["hits"]["total"]["value"]}
                self.count_cache = (time.monotonic() + 30, result)
            return JSONResponse(result, headers=headers)
        except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
            return JSONResponse({"error": {"message": "Unsupported search request. Use at most 1,000 results and refine your filters."}}, status_code=400, headers=headers)
        except (ToolError, TimeoutError):
            return JSONResponse({"error": {"message": "Search is busy or temporarily unavailable. Please retry shortly."}}, status_code=503, headers={**headers, "Retry-After": "5"})
