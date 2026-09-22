#!/usr/bin/env python3
"""Verify the public, stateless MCP contract without credentials or dependencies."""

import argparse
from datetime import datetime, timezone
import json
import urllib.request


def check(endpoint, query, host=None):
    def rpc(method, params=None):
        headers = {
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        if host:
            headers["Host"] = host
        request = urllib.request.Request(endpoint, method="POST", headers=headers,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode())
        with urllib.request.urlopen(request, timeout=30) as response:
            assert "mcp-session-id" not in response.headers, "MCP must remain stateless"
            body = json.load(response)
        assert "error" not in body, "MCP protocol error"
        return body["result"]

    init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "eqarchives-deploy-check", "version": "1.0"}})
    assert init["serverInfo"]["name"] == "EQ Archives"
    tools = {tool["name"]: tool for tool in rpc("tools/list")["tools"]}
    assert set(tools) == {"search", "fetch", "search_archive", "list_sources"}
    for tool in tools.values():
        assert tool["annotations"]["readOnlyHint"] and not tool["annotations"]["destructiveHint"]
        assert tool["_meta"]["securitySchemes"] == [{"type": "noauth"}]
        assert tool["outputSchema"]["type"] == "object"

    def call(name, arguments):
        result = rpc("tools/call", {"name": name, "arguments": arguments})
        assert not result.get("isError"), f"MCP {name} failed"
        assert len(result["content"]) == 1
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        return result["structuredContent"]

    results = call("search", {"query": query})["results"]
    assert 0 < len(results) <= 10, "Expected matching archive sources"
    first = results[0]
    document = call("fetch", {"id": first["id"]})
    assert all(document[key] == first[key] for key in ("id", "title", "url"))
    assert document["text"] and document["metadata"]["text_kind"] != "unavailable"
    assert document["url"].startswith(("https://", "http://"))
    missing = rpc("tools/call", {"name": "fetch", "arguments": {"id": "__mcp_nonexistent_smoke_document__"}})
    assert missing["isError"], "Missing documents must not fabricate source text"
    page = call("search_archive", {"query": query, "limit": 7})
    assert len(page["results"]) == 7 and page["next_offset"] == 7
    following = call("search_archive", {"query": query, "limit": 7, "offset": page["next_offset"]})
    assert len(following["results"]) == 7
    assert not {r["id"] for r in page["results"]} & {r["id"] for r in following["results"]}
    assert page["total"]["relation"] in ("eq", "gte") and page["total"]["value"] >= 14

    sources = call("list_sources", {"query": query, "limit": 1})
    assert sources["sources"] and sources["next_after"]
    next_sources = call("list_sources", {"query": query, "limit": 1, "after": sources["next_after"]})
    assert next_sources["sources"]
    source = sources["sources"][0]["value"]
    assert source != next_sources["sources"][0]["value"]
    filtered = call("search_archive", {"query": query, "filters": {"domains": [source]}, "limit": 2})
    assert filtered["results"] and all(r["metadata"]["domain_name"] == source for r in filtered["results"])
    assert call("search_archive", {"query": query, "filters": {"domains": ["__mcp_missing_source__.invalid"]}})["results"] == []

    dated = next(r for r in page["results"] if r["metadata"].get("capture_date"))
    timestamp = datetime.fromisoformat(dated["metadata"]["capture_date"].replace("Z", "+00:00"))
    day = timestamp.replace(tzinfo=timestamp.tzinfo or timezone.utc).astimezone(timezone.utc).date()
    date_page = call("search_archive", {"query": query, "filters": {"date_from": str(day), "date_to": str(day)},
                                      "sort": "capture_date_asc", "limit": 2})
    assert date_page["results"]
    for result in date_page["results"]:
        timestamp = datetime.fromisoformat(result["metadata"]["capture_date"].replace("Z", "+00:00"))
        assert timestamp.replace(tzinfo=timestamp.tzinfo or timezone.utc).astimezone(timezone.utc).date() == day
    print('MCP schemas, search/fetch, 14 distinct paged sources, source discovery, exact source filters and inclusive dates passed.')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint")
    parser.add_argument("--query", default="ancient cyclops")
    parser.add_argument("--host", help="Override Host for an isolated ingress routing check")
    args = parser.parse_args()
    check(args.endpoint, args.query, args.host)
