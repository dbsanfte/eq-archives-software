#!/usr/bin/env python3
"""Real slow-socket regressions against the isolated CI MCP/ES fixture only."""
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def check(endpoint):
    url = urlsplit(endpoint)
    assert url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost")
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-11-25"}
    def message(method, params=None):
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    def post(path, body):
        request = urllib.request.Request(f"http://{url.netloc}{path}", data=body, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, json.loads(response.read(4096))
    def call(method, params=None):
        return post("/mcp", message(method, params))

    status, _, data = call("tools/call", {"name": "fetch", "arguments": {"id": "__mcp_admission_oversized__"}})
    assert status == 413 and "result" not in data and "too large" in data["error"]["message"]
    browser_path = "/elasticsearch/eq-archive/_search"
    stages = [("/mcp", message("tools/call", {"name": "fetch", "arguments": {"id": "__mcp_admission_large__"}}),
               lambda: call("ping")),
              (browser_path, json.dumps({"size": 1, "query": {"ids": {"values": ["__mcp_admission_browser__"]}},
                "_source": ["text_full"]}).encode(), lambda: post(browser_path, b'{"size":0}'))]
    for path, body, probe in stages:
        sockets = []
        try:
            for _ in range(2):
                client = socket.socket()
                sockets.append(client)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
                client.settimeout(5)
                client.connect((url.hostname, url.port))
                fields = {**headers, "Host": url.netloc, "Content-Length": str(len(body))}
                head = f"POST {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
                client.sendall(head.encode() + body)
                received = bytearray()
                while not received.endswith(b"\r\n\r\n"):
                    part = client.recv(1)
                    assert part and len(received) < 16384, "Incomplete response headers"
                    received.extend(part)
                assert received.startswith(b"HTTP/1.1 200 "), received.decode()
                assert any(line.lower().startswith(b"content-length:") and int(line.split(b":", 1)[1]) > 5 * 1024 * 1024
                           for line in received.split(b"\r\n")), received.decode()
                # Leave the multi-megabyte body unread, holding actual TCP backpressure.
            for _ in range(14):
                status, response_headers, _ = probe()
                assert status == 503 and response_headers["Retry-After"] == "5", f"Slow readers bypassed admission: {path} ({status})"
            with urllib.request.urlopen(f"http://{url.netloc}/healthz", timeout=5) as response:
                assert response.status == 200, "Health probe blocked by research traffic"
            if path == browser_path:
                assert call("ping")[0] == 200, "Browser transfers blocked independent MCP admission"
        finally:
            for client in sockets:
                client.close()
        for _ in range(20):
            status, _, _ = probe()
            if status == 200:
                break
            time.sleep(.1)
        else:
            raise AssertionError(f"Admission did not recover after client disconnect: {path}")
    print("MCP output bounds, MCP/browser slow-socket admission, independent health and disconnect recovery passed.")


if __name__ == "__main__":
    check(sys.argv[1])
