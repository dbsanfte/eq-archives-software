#!/usr/bin/env python3
"""Verify the public, stateless MCP contract without credentials or dependencies."""

import argparse
import json
import urllib.request


def check(endpoint, query):
    def rpc(method, params=None):
        request = urllib.request.Request(endpoint, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode())
        with urllib.request.urlopen(request, timeout=30) as response:
            assert "mcp-session-id" not in response.headers, "MCP must remain stateless"
            body = json.load(response)
        assert "error" not in body, "MCP protocol error"
        return body["result"]

    init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "eqarchives-deploy-check", "version": "1.0"}})
    assert init["serverInfo"]["name"] == "EQ Archives"
    tools = {tool["name"]: tool for tool in rpc("tools/list")["tools"]}
    assert set(tools) == {"search", "fetch"}
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
    print(f'MCP handshake, schemas, search, full-text fetch and missing-source checks passed ({len(results)} results).')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint")
    parser.add_argument("--query", default="ancient cyclops")
    args = parser.parse_args()
    check(args.endpoint, args.query)
