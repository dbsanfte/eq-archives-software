import asyncio
import json
from unittest.mock import patch

import httpx
import httpx2
import pytest
from jsonschema import validate
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from eqarchives_mcp.server import create_app
from test_archive import HIT, make_archive


HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}


def rpc(client, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        message["id"] = request_id
    return client.post("/mcp", headers=HEADERS, json=message)


def test_handshake_discovery_search_fetch_and_structured_output_over_http():
    with TestClient(create_app(make_archive())) as client:
        init = rpc(client, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "research-test", "version": "1.0"}})
        assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "EQ Archives"
        assert "mcp-session-id" not in init.headers
        assert rpc(client, "notifications/initialized", request_id=None).status_code == 202
        listed = rpc(client, "tools/list").json()["result"]["tools"]
        tools = {tool["name"]: tool for tool in listed}
        assert set(tools) == {"search", "fetch"}
        for tool in tools.values():
            assert tool["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
            assert tool["_meta"]["securitySchemes"] == [{"type": "noauth"}]
            assert tool["outputSchema"]["type"] == "object"
        assert tools["search"]["inputSchema"]["required"] == ["query"]
        assert tools["fetch"]["inputSchema"]["required"] == ["id"]
        for name, arguments in (("search", {"query": "ancient cyclops"}), ("fetch", {"id": HIT["_id"]})):
            response = rpc(client, "tools/call", {"name": name, "arguments": arguments})
            assert response.status_code == 200
            result = response.json()["result"]
            assert not result.get("isError")
            assert len(result["content"]) == 1 and result["content"][0]["type"] == "text"
            assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
            validate(result["structuredContent"], tools[name]["outputSchema"])
        assert result["structuredContent"]["text"] == HIT["_source"]["text_full"]
        assert client.get("/healthz").text == "ok\n"
        assert client.get("/mcp", headers=HEADERS).status_code == 405
        assert client.delete("/mcp", headers=HEADERS).status_code == 405


@pytest.mark.parametrize("name,args", [("search", {}), ("fetch", {}), ("search", {"query": 3}),
    ("search", {"query": "x" * 1001}), ("fetch", {"id": "x" * 2049}),
    ("search", {"query": "   "}), ("fetch", {"id": ""}), ("delete", {"id": "1"})])
def test_invalid_input_and_write_tools_are_rejected(name, args):
    with TestClient(create_app(make_archive())) as client:
        result = rpc(client, "tools/call", {"name": name, "arguments": args}).json()["result"]
        assert result["isError"]


def test_transport_blocks_bad_origins_hosts_content_types_and_large_bodies():
    with TestClient(create_app(make_archive())) as client:
        for origin in ("https://chatgpt.com", "https://claude.ai", "https://search.eqarchives.org", "http://localhost:6274"):
            response = client.post("/mcp", headers={**HEADERS, "Origin": origin}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 200
        assert client.post("/mcp", headers={**HEADERS, "Origin": "https://attacker.invalid"}, json={}).status_code == 403
        assert client.get("/mcp", headers={**HEADERS, "Origin": "https://attacker.invalid"}).status_code == 403
        assert client.post("/mcp", headers={**HEADERS, "Host": "attacker.invalid"}, json={}).status_code == 421
        assert client.post("/mcp", headers=HEADERS, content="not JSON").status_code == 400
        assert client.post("/mcp", headers=HEADERS, json={"input": "x" * 17000}).status_code == 413


def test_production_clients_live_across_requests_and_close_on_shutdown():
    archive = make_archive()
    with patch("eqarchives_mcp.server.Archive.connect", return_value=archive) as connect:
        with TestClient(create_app()) as client:
            for _ in range(3):
                result = rpc(client, "tools/call", {"name": "fetch", "arguments": {"id": HIT["_id"]}}).json()["result"]
                assert not result.get("isError") and not archive.es.is_closed
            connect.assert_called_once()
        assert archive.es.is_closed and archive.embeddings.is_closed


def test_upstream_auth_failure_never_exposes_credentials_to_mcp_client():
    archive = make_archive(lambda r: httpx.Response(401, text="Bearer private-test-key password"))
    with TestClient(create_app(archive)) as client:
        response = rpc(client, "tools/call", {"name": "fetch", "arguments": {"id": "some/id"}})
        assert response.json()["result"]["isError"]
        assert "private-test-key" not in response.text and "password" not in response.text


def test_official_sdk_client_can_discover_search_and_fetch():
    async def exercise():
        app = create_app(make_archive())
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as client:
                async with streamable_http_client("http://testserver/mcp", http_client=client) as streams:
                    async with ClientSession(*streams) as session:
                        assert (await session.initialize()).server_info.name == "EQ Archives"
                        assert {tool.name for tool in (await session.list_tools()).tools} == {"search", "fetch"}
                        result = await session.call_tool("search", {"query": "ancient cyclops"})
                        assert not result.is_error
                        document = await session.call_tool("fetch", {"id": result.structured_content["results"][0]["id"]})
                        assert not document.is_error and document.structured_content["text"] == HIT["_source"]["text_full"]

    asyncio.run(exercise())
