import asyncio
import json

import httpx
import pytest
from starlette.testclient import TestClient

from eqarchives_mcp.server import create_app
from test_archive import HIT, make_archive
from test_protocol import HEADERS, rpc


def scope():
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/mcp", "raw_path": b"/mcp",
            "query_string": b"", "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                        (b"accept", b"application/json, text/event-stream"),
                        (b"mcp-protocol-version", b"2025-11-25")]}


def receiver(body):
    pending = True
    async def receive():
        nonlocal pending
        if pending:
            pending = False
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()
    return receive


@pytest.mark.parametrize("name", ["search", "search_archive", "list_sources"])
@pytest.mark.parametrize("query", ["a/" * 101, "漢" * 101, '"' + "a " * 101 + '"'])
def test_all_mcp_search_tools_reject_excessive_analysis_before_upstream(name, query):
    calls = []
    archive = make_archive(lambda r: calls.append(r), lambda r: calls.append(r))
    with TestClient(create_app(archive)) as client:
        result = rpc(client, "tools/call", {"name": name, "arguments": {"query": query}}).json()["result"]
        assert result["isError"]
        assert not calls


def test_mcp_holds_process_capacity_until_slow_responses_finish_and_cancel_recovers():
    async def exercise():
        calls = []
        def es(request):
            calls.append(request)
            return httpx.Response(200, json={"hits": {"hits": [HIT]}})
        archive = make_archive(es)
        app = create_app(archive)
        release = asyncio.Event()
        tasks = []
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "fetch", "arguments": {"id": HIT["_id"]}}}).encode()
        async with app.router.lifespan_context(app):
            try:
                for _ in range(2):
                    entered = asyncio.Event()
                    async def slow_send(message, entered=entered):
                        if message["type"] == "http.response.body":
                            entered.set()
                            await release.wait()
                    tasks.append(asyncio.create_task(app(scope(), receiver(body), slow_send)))
                    await asyncio.wait_for(entered.wait(), 2)
                sent = []
                async def send(message): sent.append(message)
                await asyncio.wait_for(app(scope(), receiver(body), send), 2)
                assert sent[0]["status"] == 503 and len(calls) == 2
                tasks[0].cancel()
                await asyncio.gather(tasks[0], return_exceptions=True)
                sent.clear()
                await asyncio.wait_for(app(scope(), receiver(body), send), 2)
                assert sent[0]["status"] == 200 and len(calls) == 3
                assert archive.requests._value == 4
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(exercise())


def test_modern_subscription_is_rejected_without_starting_an_sse_stream():
    async def exercise():
        app = create_app(make_archive())
        request_scope = scope()
        request_scope["headers"] = [(k, v) for k, v in request_scope["headers"] if k != b"mcp-protocol-version"]
        request_scope["headers"] += [(b"mcp-protocol-version", b"2026-07-28"), (b"mcp-method", b"subscriptions/listen")]
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "subscriptions/listen", "params": {
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                      "io.modelcontextprotocol/clientCapabilities": {}},
            "notifications": {"toolsListChanged": True}}}).encode()
        entered = asyncio.Event()
        sent = []
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body": entered.set()
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(request_scope, receiver(body), send))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert sent[0]["status"] == 400
                assert b"text/event-stream" not in dict(sent[0]["headers"]).get(b"content-type", b"")
                assert json.loads(sent[-1]["body"])["error"]["code"] == -32601
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_mcp_rejects_oversized_serialized_output_instead_of_truncating_source():
    hit = {"_id": HIT["_id"], "_source": {**HIT["_source"], "text_full": "a" * (4 * 1024 * 1024)}}
    archive = make_archive(lambda r: httpx.Response(200, json={"hits": {"hits": [hit]}}))
    with TestClient(create_app(archive)) as client:
        response = rpc(client, "tools/call", {"name": "fetch", "arguments": {"id": HIT["_id"]}})
        assert response.status_code == 413
        assert "too large" in response.json()["error"]["message"]
        assert "result" not in response.json()
        assert len(response.content) < 1024
        assert rpc(client, "ping").status_code == 200


@pytest.mark.parametrize("raw", [b'[]', b'null', b'{', b'{"method":"ping","method":"tools/list"}',
                                  b'{"method":"ping","id":NaN}', b'{"method":"ping","id":"\\ud800"}',
                                  b'[' * 1100 + b']' * 1100])
def test_bad_json_fails_before_sdk_or_upstream_and_does_not_echo_payload(raw):
    calls = []
    with TestClient(create_app(make_archive(lambda r: calls.append(r)))) as client:
        response = client.post("/mcp", content=raw, headers={**HEADERS, "Content-Type": "application/json"})
        assert response.status_code == 400 and response.json()["error"]["code"] == -32700
        assert not calls


@pytest.mark.parametrize("method", ["subscriptions/listen", "resources/read", "prompts/get", "tasks/list", [], None])
def test_unserved_methods_are_rejected_before_default_sdk_handlers(method, caplog):
    with TestClient(create_app(make_archive())) as client:
        response = rpc(client, method, {"name": "private-payload-marker"})
        assert response.status_code == 400 and response.json()["error"]["code"] == -32601
        assert "private-payload-marker" not in response.text + caplog.text


def test_supported_modern_discovery_and_fetch_keep_the_contract_and_honest_capabilities():
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {}}
    with TestClient(create_app(make_archive())) as client:
        def modern(method, params):
            return client.post("/mcp", headers={**HEADERS, "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": method, **({"Mcp-Name": params["name"]} if "name" in params else {})},
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": meta, **params}})
        legacy = rpc(client, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                     "clientInfo": {"name": "test", "version": "1"}}).json()["result"]
        discovered = modern("server/discover", {}).json()["result"]
        for info in (legacy, discovered):
            assert info["capabilities"] == {"tools": {"listChanged": False}}
        assert legacy["serverInfo"]["name"] == "EQ Archives"
        assert "2026-07-28" in discovered["supportedVersions"]
        assert len(modern("tools/list", {}).json()["result"]["tools"]) == 4
        response = modern("tools/call", {"name": "fetch", "arguments": {"id": HIT["_id"]}})
        result = response.json()["result"]
        assert response.status_code == 200 and not result.get("isError")
        assert result["structuredContent"]["text"] == HIT["_source"]["text_full"]


def test_fragmented_response_bounds_and_unexpected_streams_are_fail_closed(monkeypatch):
    from eqarchives_mcp import mcp_boundary as boundary
    from mcp.server.transport_security import TransportSecuritySettings
    monkeypatch.setattr(boundary, "MAX_RESPONSE_BYTES", 32)
    async def exercise(parts, content_type, status):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", content_type), (b"content-length", b"1"),
                                    (b"cache-control", b"public")]})
            for i, part in enumerate(parts):
                await send({"type": "http.response.body", "body": part, "more_body": i < len(parts) - 1})
        guard = boundary.MCPBoundary(app, TransportSecuritySettings(allowed_hosts=["testserver"]))
        sent = []
        async def send(message): sent.append(message)
        await guard(scope(), receiver(b'{"jsonrpc":"2.0","id":1,"method":"ping"}'), send)
        assert sent[0]["status"] == status
        assert dict(sent[0]["headers"])[b"cache-control"] == b"no-store"
        if status == 200:
            assembled = b"".join(m["body"] for m in sent[1:])
            assert assembled == b"".join(parts) and not sent[-1]["more_body"]
            assert int(dict(sent[0]["headers"])[b"content-length"]) == len(assembled)
        else:
            assert json.loads(sent[1]["body"])["id"] == 1
    asyncio.run(exercise([b'{"ok":', b'true}'], b"application/json", 200))
    asyncio.run(exercise([b'x' * 20, b'x' * 20, b'x'], b"application/json", 413))
    asyncio.run(exercise([b'data: x\n\n'], b"text/event-stream", 413))


def test_mcp_upload_deadline_disconnect_and_partial_send_release_capacity(monkeypatch):
    from eqarchives_mcp import mcp_boundary as boundary
    from mcp.server.transport_security import TransportSecuritySettings
    monkeypatch.setattr(boundary, "BODY_TIMEOUT", .01)
    monkeypatch.setattr(boundary, "REQUEST_TIMEOUT", .03)
    async def exercise(mode):
        async def app(scope, receive, send):
            if mode == "passthrough":
                return await send({"passed": True})
            await receive()
            if mode == "second_receive":
                await receive()
            if mode == "work":
                await asyncio.sleep(1)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{}'})
        guard = boundary.MCPBoundary(app, TransportSecuritySettings(allowed_hosts=["testserver"]))
        sent = []
        async def send(message):
            sent.append(message)
            if mode == "send" and message["type"] == "http.response.body":
                await asyncio.sleep(1)
        async def receive():
            if mode == "disconnect": return {"type": "http.disconnect"}
            await asyncio.sleep(1)
        s = {"type": "lifespan"} if mode == "passthrough" else scope()
        source = receive if mode in ("upload", "disconnect") else receiver(b'{"method":"ping"}')
        if mode == "send":
            with pytest.raises(TimeoutError): await guard(s, source, send)
            assert sent[0]["status"] == 200
        else:
            await guard(s, source, send)
            if mode == "disconnect": assert not sent
            elif mode == "passthrough": assert sent == [{"passed": True}]
            else: assert sent[0]["status"] == 503
        assert guard.slots._value == 2
    for mode in ("upload", "disconnect", "work", "send", "second_receive", "passthrough"):
        asyncio.run(exercise(mode))


def test_mcp_total_deadline_cancels_sdk_work_and_releases_retrieval(monkeypatch):
    from eqarchives_mcp import mcp_boundary as boundary
    monkeypatch.setattr(boundary, "REQUEST_TIMEOUT", .03)
    async def es(request):
        await asyncio.sleep(1)
    archive = make_archive(es)
    with TestClient(create_app(archive)) as client:
        response = rpc(client, "tools/call", {"name": "fetch", "arguments": {"id": "x"}})
        assert response.status_code == 503 and response.headers["retry-after"] == "5"
        assert archive.requests._value == 4
        assert rpc(client, "ping").status_code == 200
