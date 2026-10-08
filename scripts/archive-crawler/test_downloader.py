"""Exercise the real Ruby downloader over a persistent HTTP/1.1 fixture."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.connections = 0
        self.requests = []
        self.statuses = []
        self.catalog = [["timestamp", "original", "mimetype", "statuscode", "digest", "length"],
                        ["20000101000000", "http://guild.example/Guide%2FOne?a=1&b=2", "text/html", "200", "ABCD", "80"]]
        self.body = b"<title>EQ guild</title><p>EverQuest guild history</p>"
        self.redirect = None
        self.memento = "Sat, 01 Jan 2000 00:00:00 GMT"
        super().__init__(("127.0.0.1", 0), Handler)

    def get_request(self):
        socket, address = super().get_request()
        self.connections += 1
        return socket, address


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        server.requests.append((self.path, time.monotonic()))
        status = server.statuses.pop(0) if server.statuses else 200
        is_catalog = self.path.startswith("/cdx/")
        if server.redirect and not is_catalog:
            status = 302
        data = json.dumps(server.catalog).encode() if is_catalog else server.body
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", "application/json" if is_catalog else "text/html; charset=utf-8")
        if server.redirect and not is_catalog:
            self.send_header("Location", server.redirect)
        if not is_catalog and server.memento:
            self.send_header("Memento-Datetime", server.memento)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class DownloaderTests(unittest.TestCase):
    url = "http://guild.example/Guide%2FOne?a=1&b=2"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "capture.html"
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.process = subprocess.Popen(["ruby", str(Path(__file__).with_name("downloader.rb"))],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close)
        self.configure()

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def configure(self, **bounds):
        result = self.call({"op": "configure", "origin": "http://127.0.0.1:" + str(self.server.server_port),
                            "delay": 0.04, "bytes_per_second": 10000000, "max_requests": 20,
                            "max_response_bytes": 4096, "max_total_bytes": 8192, "max_seconds": 60, **bounds})
        self.assertTrue(result["ok"], result)

    def call(self, job):
        self.process.stdin.write(json.dumps(job) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        self.assertTrue(line, "Ruby downloader stopped unexpectedly")
        return json.loads(line)

    def job(self, operation):
        return {"op": operation, "url": self.url, "from": "19990101000000", "to": "20011231235959",
                "timestamp": "20000101000000", "destination": str(self.destination)}

    def test_persistent_connection_throttle_identity_and_real_capture(self):
        listing = self.call(self.job("list"))
        self.assertTrue(listing["ok"], listing)
        self.assertEqual(listing["result"]["captures"][0]["url"], self.url)
        captured = self.call(self.job("capture"))
        self.assertTrue(captured["ok"], captured)
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(self.destination.read_bytes(), self.server.body)
        self.assertEqual(captured["result"]["timestamp_basis"], "memento_datetime")
        self.assertGreaterEqual(self.server.requests[1][1] - self.server.requests[0][1], 0.03)
        params = parse_qs(urlsplit(self.server.requests[0][0]).query)
        self.assertEqual(params["url"], [self.url])
        self.assertIn("%2FOne?a=1&b=2", self.server.requests[1][0])

    def test_422_backoff_keeps_persistent_connection(self):
        self.server.statuses = [422, 200]
        start = time.monotonic()
        result = self.call(self.job("list"))
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(time.monotonic() - start, 5)
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(result["transport"]["requests"], 2)

    def test_ezboard_paginated_catalog_keeps_all_versions_and_encoded_resume(self):
        url = 'http://pub4.ezboard.com/feqasylum'
        message = url + 'general.showMessageRange?topicID=391.topic&start=21&stop=40'
        key = 'com%2Cezboard%2Cpub4%29%2Ffeqasylum+20010101000000%21'
        self.server.catalog = [self.server.catalog[0],
            ['20000101000000', message, 'text/html', '200', 'SAME', '123'],
            ['20010101000000', message, 'text/html', '200', 'SAME', '123'], [], [key]]
        job = {**self.job('ezboard_list'), 'url': url}
        first = self.call(job)
        self.assertTrue(first['ok'], first)
        self.assertEqual(len(first['result']['captures']), 2)
        self.assertEqual(first['result']['resume_key'], key)
        self.server.catalog = [self.server.catalog[0]]
        second = self.call({**job, 'resume_key': key})
        self.assertTrue(second['ok'], second)
        params = parse_qs(urlsplit(self.server.requests[-1][0]).query)
        self.assertEqual(params['resumeKey'], ['com,ezboard,pub4)/feqasylum 20010101000000!'])
        self.assertEqual(params['matchType'], ['prefix'])
        self.assertNotIn('collapse', params)
        self.assertEqual(self.server.connections, 1)

    def test_ezboard_catalog_rejects_broad_scopes_and_malformed_continuations(self):
        for url in ('http://pub4.ezboard.com/', 'http://evil.example/feqasylum',
                    'http://pub4.ezboard.com:8080/feqasylum', 'http://pub4.ezboard.com/feqasylum*'):
            result = self.call({**self.job('ezboard_list'), 'url': url})
            self.assertFalse(result['ok'], result)
        self.assertEqual(len(self.server.requests), 0)
        self.server.catalog = [self.server.catalog[0], ['bad footer']]
        result = self.call({**self.job('ezboard_list'), 'url': 'http://server3.ezboard.com/btest'})
        self.assertFalse(result['ok'])

    def test_empty_listing_is_known_unavailable_but_malformed_is_unresolved(self):
        self.server.catalog = []
        result = self.call(self.job("list"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["captures"], [])
        self.server.catalog = {"error": "upstream detail"}
        self.assertFalse(self.call(self.job("list"))["ok"])

    def test_budget_and_oversized_capture_leave_no_partial_artifact(self):
        self.configure(max_requests=1)
        self.assertTrue(self.call(self.job("list"))["ok"])
        result = self.call(self.job("capture"))
        self.assertFalse(result["ok"])
        self.assertIn("budget", result["error"])
        self.assertFalse(self.destination.exists())
        self.configure(max_response_bytes=10)
        result = self.call(self.job("capture"))
        self.assertFalse(result["ok"])
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_suffix(".html.part").exists())

    def test_bandwidth_cap_slows_body_and_out_of_tier_is_rejected(self):
        self.configure(bytes_per_second=100)
        start = time.monotonic()
        self.assertTrue(self.call(self.job("capture"))["ok"])
        self.assertGreaterEqual(time.monotonic() - start, len(self.server.body) / 100)
        self.destination.unlink()
        self.server.memento = "Tue, 01 Jan 2008 00:00:00 GMT"
        result = self.call(self.job("capture"))
        self.assertFalse(result["ok"])
        self.assertIn("outside", result["error"])
        self.assertFalse(self.destination.exists())

    def test_off_archive_redirect_is_rejected_without_following(self):
        self.server.redirect = "https://different.example/"
        result = self.call(self.job("capture"))
        self.assertFalse(result["ok"])
        self.assertIn("outside Wayback", result["error"])
        self.assertEqual(len(self.server.requests), 1)

    def test_cdx_does_not_merge_scheme_or_query_identity(self):
        self.server.catalog += [["20000101000000", self.url.replace("http:", "https:"), "text/html", "200", "HTTPS", "80"],
                                ["20000101000000", self.url.replace("%2F", "/"), "text/html", "200", "DECODED", "80"],
                                [], ["resume-key"]]
        result = self.call(self.job("list"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["result"]["captures"]), 1)
        self.assertTrue(result["result"]["listing_limited"])

    def test_historical_explicit_default_port_preserves_cdx_original_spelling(self):
        original = self.url.replace('guild.example/', 'guild.example:80/')
        self.server.catalog[1][1] = original
        listing = self.call(self.job("list"))
        self.assertTrue(listing["ok"], listing)
        self.assertEqual(listing["result"]["captures"][0]["url"], original)
        captured = self.call(self.job("capture") | {"url": original})
        self.assertTrue(captured["ok"], captured)
        self.assertEqual(captured["result"]["url"], original)

    def test_vendor_files_match_pinned_upstream_and_preserve_license(self):
        root = Path(__file__).parent / "vendor" / "wayback-machine-downloader"
        upstream = json.loads((root / "UPSTREAM.json").read_text())
        for path, expected in upstream["files"].items():
            self.assertEqual(hashlib.sha256((root / path).read_bytes()).hexdigest(), expected)
        self.assertIn("Permission is hereby granted", (root / "MIT-LICENSE.txt").read_text())


if __name__ == "__main__":
    unittest.main()
