"""Exercise the real Ruby downloader over a persistent HTTP/1.1 fixture."""

import hashlib
import gzip
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
        self.catalogs = {}
        self.replays = {}
        self.memento = "Sat, 01 Jan 2000 00:00:00 GMT"
        self.chunk_delay = 0
        super().__init__(("127.0.0.1", 0), Handler)

    def get_request(self):
        socket, address = super().get_request()
        self.connections += 1
        return socket, address


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass  # Budget checks intentionally abort partially read responses.

    def do_GET(self):
        server = self.server
        server.requests.append((self.path, time.monotonic()))
        status = server.statuses.pop(0) if server.statuses else 200
        is_catalog = self.path.startswith("/cdx/")
        if server.redirect and not is_catalog:
            status = 302
        params = parse_qs(urlsplit(self.path).query)
        catalog = server.catalogs.get((params.get('url', [''])[0], 'statuscode:200' in params.get('filter', [])), server.catalog)
        replay = server.replays.get(self.path, {})
        status = replay.get('status', status)
        data = json.dumps(catalog).encode() if is_catalog else replay.get('body', server.body)
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", "application/json" if is_catalog else replay.get('content_type', 'text/html; charset=utf-8'))
        if replay.get('encoding'):
            self.send_header('Content-Encoding', replay['encoding'])
        if server.redirect and not is_catalog:
            self.send_header("Location", server.redirect)
        elif replay.get('location'):
            self.send_header('Location', replay['location'])
        if not is_catalog and server.memento:
            self.send_header("Memento-Datetime", server.memento)
        self.end_headers()
        try:
            if server.chunk_delay and not is_catalog:
                for offset in range(0, len(data), 16):
                    self.wfile.write(data[offset:offset + 16])
                    self.wfile.flush()
                    time.sleep(server.chunk_delay)
            else:
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

    def test_default_upstream_pacing_keeps_cdx_and_replays_on_one_connection(self):
        # No explicit rate/delay: production defaults must not add fixed sleeps.
        configured = self.call({'op': 'configure', 'origin': 'http://127.0.0.1:' + str(self.server.server_port)})
        self.assertTrue(configured['ok'], configured)
        for operation in ('list', 'capture_file', 'list'):
            result = self.call(self.job(operation))
            self.assertTrue(result['ok'], result)
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(result['transport']['requests'], 3)
        self.assertLess(self.server.requests[-1][1] - self.server.requests[0][1], 1.5)
        self.assertEqual(self.destination.read_bytes(), self.server.body)

    def test_empty_html_listing_reports_archived_redirects_and_errors(self):
        root = 'http://www.solusekro.com/'
        header = self.server.catalog[0]
        self.server.catalogs[(root, True)] = []
        self.server.catalogs[(root, False)] = [header,
            ['20000118215840', root.replace('.com/', '.com:80/'), 'text/html', '302', 'REDIRECT', '345'],
            ['20001109055200', root, 'text/html', '500', 'ERROR', '220'],
            ['20000118215840', root.replace('www.', ''), 'text/html', '302', 'OTHER', '345']]
        result = self.call({**self.job('list'), 'url': root})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['captures'], [])
        self.assertEqual([r['timestamp'] for r in result['result']['redirects']], ['20000118215840'])
        self.assertEqual(result['result']['response_types'], [['302', 'text/html'], ['500', 'text/html']])
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(result['transport']['requests'], 2)

    def test_redirect_resolution_retains_chain_and_never_relables_capture_source(self):
        root = 'http://www.solusekro.com:80/'
        target = 'http://www.solusekro.com/eq/'
        start = '/web/20000101000000id_/' + root
        finish = '/web/20000101000000id_/' + target
        self.server.replays[start] = {'status':302, 'location':finish, 'body':b'Redirect'}
        result = self.call({**self.job('resolve'), 'url':root})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['url'], target)
        self.assertEqual(result['result']['requested_url'], root)
        self.assertEqual(result['result']['redirects'], [{'from':start, 'to':finish, 'status':302}])
        self.assertEqual(result['result']['timestamp'], '20000101000000')
        self.assertFalse(self.destination.exists())
        # Ordinary capture must still reject substituting the target for the root.
        capture = self.call({**self.job('capture'), 'url':root})
        self.assertFalse(capture['ok'])
        self.assertIn('different original URL', capture['error'])
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.server.connections, 1)

    def test_redirect_diagnostics_cannot_turn_a_request_limit_into_absence(self):
        self.configure(max_requests=1)
        self.server.catalog=[]
        result=self.call(self.job('list'))
        self.assertFalse(result['ok'])
        self.assertIn('budget', result['error'])

    def test_scope_catalog_preserves_binary_files_and_equal_digest_dates(self):
        self.server.catalog = [self.server.catalog[0],
            ['19990101000000', 'http://guild.example/images/a.png', 'image/png', '200', 'SAME', '30'],
            ['20011231235959', 'http://guild.example/images/a.png', 'image/png', '200', 'SAME', '30'],
            ['20000101000000', 'http://guild.example/files/a.zip', 'application/zip', '200', 'OTHER', '50']]
        result = self.call({**self.job('scope_list'), 'url': 'http://guild.example/', 'match': 'prefix'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['result']['captures']), 3)
        params = parse_qs(urlsplit(self.server.requests[0][0]).query)
        self.assertEqual(params['filter'], ['statuscode:200'])
        self.assertNotIn('collapse', params)
        self.assertEqual(params['matchType'], ['prefix'])
        invalid = self.call({**self.job('scope_list'), 'url': 'http://guild.example/*', 'match': 'prefix'})
        self.assertFalse(invalid['ok'])

    def test_large_streamed_binary_and_gzip_keep_exact_bytes_and_connection(self):
        raw = bytes(range(256)) * 14000  # exceeds the old 1 MiB source limit
        self.configure(max_response_bytes=8*1024**2, max_total_bytes=12*1024**2, bytes_per_second=100000000)
        path = '/web/20000101000000id_/' + self.url
        for body, encoding in [(raw, None), (gzip.compress(raw), 'gzip')]:
            self.server.replays[path] = {'body': body, 'encoding': encoding, 'content_type': 'application/zip'}
            result = self.call(self.job('capture_file'))
            self.assertTrue(result['ok'], result)
            self.assertEqual(self.destination.read_bytes(), raw)
            self.assertEqual(result['result']['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(result['result']['bytes'], len(raw))
            self.assertEqual(result['result']['content_type'], 'application/zip')
        self.assertEqual(self.server.connections, 1)
        self.assertFalse(self.destination.with_suffix('.html.part').exists())

    def test_streaming_budget_does_not_save_partial_or_substitute_nearest_version(self):
        self.configure(max_response_bytes=8192, max_total_bytes=128)
        self.server.body = b'x' * 4096
        result = self.call(self.job('capture_file'))
        self.assertFalse(result['ok'])
        self.assertIn('budget', result['error'])
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_suffix('.html.part').exists())
        self.configure()
        self.server.memento = 'Sun, 02 Jan 2000 00:00:00 GMT'
        self.server.body = b'body'
        result = self.call(self.job('capture_file'))
        self.assertFalse(result['ok'])
        self.assertIn('different dated version', result['error'])
        self.assertFalse(self.destination.exists())

    def test_zero_length_archived_file_is_preserved(self):
        self.server.body = b''
        result = self.call(self.job('capture_file'))
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.destination.read_bytes(), b'')
        self.assertEqual(result['result']['bytes'], 0)

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

    def test_full_window_exact_catalog_retains_equal_content_versions_and_paginates(self):
        job = {**self.job('capture_list'), 'to': '20061231235959'}
        self.server.catalog = [self.server.catalog[0],
            ['19990101000000', self.url, 'text/html', '200', 'SAME', '123'],
            ['20061231235959', self.url, 'text/html', '200', 'SAME', '123'],
            ['20061231235959', self.url.replace('http:', 'https:'), 'text/html', '200', 'OTHER', '123'],
            [], ['resume%2Bkey']]
        listing = self.call(job)
        self.assertTrue(listing['ok'], listing)
        self.assertEqual([c['timestamp'] for c in listing['result']['captures']], ['19990101000000','20061231235959'])
        self.assertEqual(listing['result']['resume_key'], 'resume%2Bkey')
        self.server.catalog = [self.server.catalog[0]]
        self.assertTrue(self.call({**job,'resume_key':'resume%2Bkey'})['ok'])
        params = parse_qs(urlsplit(self.server.requests[-1][0]).query)
        self.assertEqual(params['matchType'], ['exact'])
        self.assertEqual(params['from'], ['19990101000000'])
        self.assertEqual(params['to'], ['20061231235959'])
        self.assertEqual(params['resumeKey'], ['resume+key'])
        self.assertNotIn('collapse', params)
        self.assertEqual(self.server.connections, 1)
        self.server.catalog.append(['20070101000000', self.url, 'text/html', '200', 'LATE', '123'])
        self.assertFalse(self.call(job)['ok'])

    def test_sitepowerup_prefix_catalog_keeps_dates_queries_and_single_connection(self):
        prefix = 'http://www.sitepowerup.com/mb/view.asp?Action=Reply&BoardID=102010'
        message = prefix + '&Reply=12155'
        job = {**self.job('sitepowerup_list'), 'url':prefix, 'to':'20061231235959'}
        self.server.catalog = [self.server.catalog[0],
            ['19990101000000', message, 'text/html','200','SAME','123'],
            ['20061231235959', message+'&Page=2', 'text/html','200','SAME','123'],[],['key%2Bnext']]
        result = self.call(job)
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(result['result']['captures']),2)
        self.server.catalog = [self.server.catalog[0]]
        self.assertTrue(self.call({**job,'resume_key':'key%2Bnext'})['ok'])
        params=parse_qs(urlsplit(self.server.requests[-1][0]).query)
        self.assertEqual(params['resumeKey'],['key+next'])
        self.assertEqual(params['matchType'],['prefix'])
        self.assertNotIn('collapse',params)
        self.assertEqual(self.server.connections,1)
        before=len(self.server.requests)
        for url in ('http://www.sitepowerup.com/mb/',prefix.replace('Reply','Post'),prefix+'&BoardID=2',prefix.replace('102010','*'),prefix.replace('.com','.com.evil.example')):
            self.assertFalse(self.call({**job,'url':url})['ok'])
        self.assertEqual(len(self.server.requests),before)

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

    def test_unlimited_bandwidth_streams_large_files_and_still_enforces_byte_limits(self):
        self.server.body = b'x' * (2 * 1024**2)
        self.configure(bytes_per_second=0, max_response_bytes=3 * 1024**2,
                       max_total_bytes=5 * 1024**2, max_seconds=3)
        result = self.call(self.job('capture_file'))
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.destination.read_bytes(), self.server.body)
        self.assertEqual(result['transport']['bytes'], len(self.server.body))
        self.destination.unlink()
        self.configure(bytes_per_second=0, max_response_bytes=3 * 1024**2, max_total_bytes=1024)
        result = self.call(self.job('capture_file'))
        self.assertFalse(result['ok'])
        self.assertIn('total byte budget', result['error'])
        self.assertFalse(self.destination.exists())
        self.assertFalse(Path(str(self.destination) + '.part').exists())

    def test_unlimited_stream_checks_wall_clock_even_when_chunks_beat_read_timeout(self):
        self.server.body = b'x' * 2048
        self.server.chunk_delay = .01
        self.configure(bytes_per_second=0, max_seconds=.3)
        result = self.call(self.job('capture_file'))
        self.assertFalse(result['ok'])
        self.assertIn('wall-clock budget', result['error'])
        self.assertGreater(result['transport']['bytes'], 0)
        self.assertFalse(self.destination.exists())
        self.assertFalse(Path(str(self.destination) + '.part').exists())

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

    def test_replay_default_port_redirects_keep_catalog_identity_and_source_bytes(self):
        self.configure(max_requests=40, max_total_bytes=32768)
        for scheme, port in [('http', 80), ('https', 443)]:
            plain = self.url.replace('http:', scheme + ':')
            explicit = plain.replace('guild.example/', f'guild.example:{port}/')
            for original, replayed in [(explicit, plain), (plain, explicit)]:
                for operation in ('capture', 'capture_file'):
                    with self.subTest(scheme=scheme, original=original, operation=operation):
                        start = '/web/20000101000000id_/' + original
                        finish = '/web/20000101000000id_/' + replayed
                        self.server.replays = {start: {'status': 302, 'location': finish, 'body': b'Redirect'}}
                        result = self.call({**self.job(operation), 'url': original})
                        self.assertTrue(result['ok'], result)
                        self.assertEqual(result['result']['url'], original)
                        self.assertEqual(result['result']['replay_original_url'], replayed)
                        self.assertEqual(result['result']['timestamp'], '20000101000000')
                        self.assertEqual(result['result']['sha256'], hashlib.sha256(self.server.body).hexdigest())
                        self.assertEqual(self.destination.read_bytes(), self.server.body)
        self.assertEqual(self.server.connections, 1)

    def test_port_equivalence_does_not_accept_other_sources_or_substitute_dates(self):
        self.configure(max_requests=50, max_total_bytes=32768)
        original = self.url.replace('guild.example/', 'guild.example:80/')
        variants = [self.url.replace('guild.example/', 'guild.example:8080/'),
                    self.url.replace('guild.example/', 'guild.example:443/'),
                    self.url.replace('http:', 'https:'), self.url.replace('guild.example', 'other.example'),
                    self.url.replace('Guide', 'guide'), self.url.replace('%2F', '/'),
                    self.url.replace('a=1&b=2', 'b=2&a=1')]
        for operation in ('capture', 'capture_file'):
            for target in variants:
                with self.subTest(operation=operation, target=target):
                    self.server.replays = {'/web/20000101000000id_/' + original:
                        {'status': 302, 'location': '/web/20000101000000id_/' + target, 'body': b'Redirect'}}
                    result = self.call({**self.job(operation), 'url': original})
                    self.assertFalse(result['ok'], result)
                    self.assertIn('different original URL', result['error'])
                    self.assertFalse(self.destination.exists())
                    self.assertFalse(Path(str(self.destination) + '.part').exists())
        # Guildsay's failing replay also selects an earlier date. The complete
        # file engine must still mark that requested version unavailable.
        self.server.replays = {'/web/20000101000000id_/' + original:
            {'status': 302, 'location': '/web/19991231000000id_/' + self.url, 'body': b'Redirect'}}
        self.server.memento = 'Fri, 31 Dec 1999 00:00:00 GMT'
        result = self.call({**self.job('capture_file'), 'url': original})
        self.assertFalse(result['ok'], result)
        self.assertIn('different dated version', result['error'])
        self.assertFalse(self.destination.exists())
        self.assertFalse(Path(str(self.destination) + '.part').exists())
        result = self.call({**self.job('capture'), 'url': original})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['timestamp'], '19991231000000')
        self.assertEqual(result['result']['requested_timestamp'], '20000101000000')
        self.assertEqual(result['result']['url'], original)

    def test_vendor_files_match_pinned_upstream_and_preserve_license(self):
        root = Path(__file__).parent / "vendor" / "wayback-machine-downloader"
        upstream = json.loads((root / "UPSTREAM.json").read_text())
        for path, expected in upstream["files"].items():
            self.assertEqual(hashlib.sha256((root / path).read_bytes()).hexdigest(), expected)
        self.assertIn("Permission is hereby granted", (root / "MIT-LICENSE.txt").read_text())


if __name__ == "__main__":
    unittest.main()
