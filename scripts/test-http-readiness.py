#!/usr/bin/env python3
"""Exercise the real readiness command against a resetting startup connection."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import struct
import subprocess
import threading


class StartingServer(BaseHTTPRequestHandler):
    calls = 0
    unavailable = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).calls += 1
        if self.unavailable:
            self.send_response(503)
            self.end_headers()
            return
        if self.calls == 1:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")


with HTTPServer(("127.0.0.1", 0), StartingServer) as server:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        script = Path(__file__).with_name("wait-http.sh")
        result = subprocess.run(["bash", str(script), f"http://127.0.0.1:{server.server_port}/healthz"],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok\n" and StartingServer.calls == 2
        StartingServer.calls = 0
        StartingServer.unavailable = True
        result = subprocess.run(["bash", str(script), "--retry", "2",
                                 f"http://127.0.0.1:{server.server_port}/healthz"],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 22, result.stderr
        assert StartingServer.calls == 3
        print("HTTP readiness regression passed: startup resets recover; persistent failure exhausts retries.")
    finally:
        server.shutdown()
        thread.join()
