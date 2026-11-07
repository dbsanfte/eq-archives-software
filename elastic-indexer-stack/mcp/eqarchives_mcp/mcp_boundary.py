"""Process-wide MCP admission and a closed, bounded HTTP protocol surface."""
import asyncio
import json

from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse

from .http_limits import send_body

MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
BODY_TIMEOUT = 2
REQUEST_TIMEOUT = 30
METHODS = frozenset({"initialize", "server/discover", "ping", "tools/list", "tools/call",
                     "notifications/initialized", "notifications/cancelled"})


def failure(status, code, message, request_id=None):
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if status == 503:
        headers["Retry-After"] = "5"
    return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": code, "message": message}}, status_code=status, headers=headers)


def unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON keys")
    return result


def invalid_constant(value):
    raise ValueError("Non-finite JSON number")


class MCPBoundary:
    def __init__(self, app, security):
        self.app = app
        self.security = TransportSecurityMiddleware(security)
        self.slots = asyncio.Semaphore(2)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/mcp" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        rejected = await self.security.validate_request(request, is_post=True)
        if rejected:
            return await rejected(scope, receive, send)
        if self.slots.locked():
            return await failure(503, -32000, "Archive research is busy. Please retry shortly.")(scope, receive, send)
        started = False
        request_id = None
        async with self.slots:
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    raw = bytearray()
                    async with asyncio.timeout(BODY_TIMEOUT):
                        async for chunk in request.stream():
                            if len(raw) + len(chunk) > MAX_REQUEST_BYTES:
                                return await failure(413, -32600, "MCP request is too large.")(scope, receive, send)
                            raw.extend(chunk)
                    try:
                        value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
                        # Validate escaped surrogates too, consistently across SDK protocol eras.
                        json.dumps(value, ensure_ascii=False).encode("utf-8")
                        if not isinstance(value, dict):
                            raise ValueError("Expected one JSON-RPC message")
                    except (ValueError, RecursionError):
                        return await failure(400, -32700, "Invalid MCP JSON request.")(scope, receive, send)
                    candidate = value.get("id")
                    if type(candidate) in (str, int):
                        request_id = candidate
                    method = value.get("method")
                    if not isinstance(method, str) or method not in METHODS:
                        return await failure(400, -32601, "Unsupported MCP method.", request_id)(scope, receive, send)
                    pending = True
                    async def replay():
                        nonlocal pending
                        if pending:
                            pending = False
                            return {"type": "http.request", "body": bytes(raw), "more_body": False}
                        return await receive()

                    response = None
                    body = bytearray()
                    response_rejected = False
                    async def bounded_send(message):
                        nonlocal response, started, response_rejected
                        if response_rejected:
                            return
                        if message["type"] == "http.response.start":
                            headers = {k.lower(): v.lower() for k, v in message["headers"]}
                            if b"text/event-stream" in headers.get(b"content-type", b""):
                                response_rejected = True
                                return
                            response = message
                        elif message["type"] == "http.response.body":
                            part = message.get("body", b"")
                            if len(body) + len(part) > MAX_RESPONSE_BYTES:
                                response_rejected = True
                                body.clear()
                                return
                            body.extend(part)
                            if not message.get("more_body", False):
                                if method in ("initialize", "server/discover"):
                                    data = json.loads(body)
                                    if "result" in data:
                                        # SDK defaults can advertise features outside our public contract.
                                        data["result"]["capabilities"] = {"tools": {"listChanged": False}}
                                        body[:] = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
                                headers = [(k, v) for k, v in response["headers"]
                                           if k.lower() not in (b"content-length", b"cache-control")]
                                headers += [(b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]
                                started = True
                                await send({**response, "headers": headers})
                                await send_body(send, body)
                    await self.app(scope, replay, bounded_send)
                    if response_rejected:
                        # Do not raise from send(): SDK exception handlers can
                        # replace that rejection with their own 500 response.
                        await failure(413, -32000, "MCP response is too large or requires an unsupported stream. Open the source on the archive website.",
                                      request_id)(scope, receive, send)
            except ClientDisconnect:
                return
            except TimeoutError:
                if started:
                    raise  # Close an incomplete transfer; never label truncated JSON a success.
                await failure(503, -32000, "Archive research timed out. Please retry shortly.", request_id)(scope, receive, send)
