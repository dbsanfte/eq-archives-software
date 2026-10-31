import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import struct
import threading
import time
import unittest
from unittest.mock import patch

import benchmark


def vector(index=0):
    return [1.0 if position == index else 0.0 for position in range(768)]


def response(count=2):
    return {'model': 'nomic', 'data': [{'index': i, 'embedding': vector(i)} for i in reversed(range(count))]}


class BenchmarkTests(unittest.TestCase):
    def test_vector_identity_is_bound_to_returned_indices_for_both_encodings(self):
        for encoded in (False, True):
            data = response()
            if encoded:
                for row in data['data']:
                    row['embedding'] = base64.b64encode(struct.pack('<768f', *row['embedding'])).decode()
            self.assertEqual(benchmark.vectors(data, 2, 'nomic'), [tuple(vector(i)) if encoded else vector(i) for i in range(2)])

    def test_invalid_or_incomplete_batches_never_pass_validation(self):
        for modification in ('model', 'missing', 'duplicate', 'boolean', 'dimension', 'nan', 'norm', 'encoded'):
            with self.subTest(modification=modification):
                data = response()
                if modification == 'model': data['model'] = 'different'
                if modification == 'missing': data['data'].pop()
                if modification == 'duplicate': data['data'][0]['index'] = 0
                if modification == 'boolean': data['data'][0]['index'] = True
                if modification == 'dimension': data['data'][0]['embedding'].pop()
                if modification == 'nan': data['data'][0]['embedding'][0] = math.nan
                if modification == 'norm': data['data'][0]['embedding'] = [1.] * 768
                if modification == 'encoded': data['data'][0]['embedding'] = base64.b64encode(b'wrong').decode()
                with self.assertRaises(ValueError): benchmark.vectors(data, 2, 'nomic')

    def test_benchmark_cannot_call_a_public_paid_endpoint(self):
        with patch.object(benchmark.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('8.8.8.8', 443))]):
            with self.assertRaises(ValueError): benchmark.private_endpoint('https://api.example/v1/embeddings')
        with self.assertRaises(ValueError): benchmark.private_endpoint('http://user:password@127.0.0.1/')
        with patch.object(benchmark.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('192.168.50.193', 8081))]):
            self.assertEqual(benchmark.private_endpoint('http://192.168.50.193:8081/v1/embeddings').port, 8081)

    def test_bounded_concurrency_and_request_budget(self):
        class FakeClient:
            calls = active = maximum = 0
            lock = threading.Lock()
            def embed(self, inputs):
                with self.lock:
                    self.calls += 1; self.active += 1; self.maximum = max(self.maximum, self.active)
                time.sleep(.005)
                with self.lock: self.active -= 1
        client = FakeClient()
        result = benchmark.benchmark(client, ['text']*3, seconds=.2, concurrency=4, max_requests=7)
        self.assertEqual(client.calls, 8)  # one excluded warmup plus seven timed calls
        self.assertEqual(result['requests'], 7)
        self.assertEqual(result['batch_size'], 3)
        self.assertGreater(client.maximum, 1)
        self.assertLessEqual(client.maximum, 4)
        self.assertGreater(result['inputs_per_second'], 0)

    def test_http_connections_are_reused_and_wire_formats_are_checked(self):
        peers = set()
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_POST(self):
                peers.add(self.client_address)
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.test.assertEqual(self.headers['Authorization'], 'Bearer fixture-key')
                result = response(len(payload['input']))
                if payload['encoding_format'] == 'base64':
                    for row in result['data']:
                        row['embedding'] = base64.b64encode(struct.pack('<768f', *row['embedding'])).decode()
                raw = json.dumps(result).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.test = self
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        client = benchmark.Client(f'http://127.0.0.1:{server.server_port}/v1/embeddings', 'fixture-key', 'nomic')
        try:
            self.assertEqual(client.embed(['a', 'b']), [vector(0), vector(1)])
            self.assertEqual(client.embed(['a']), [vector(0)])
            client.encoding = 'base64'
            self.assertEqual(client.embed(['a']), [tuple(vector(0))])
            self.assertEqual(len(peers), 1)
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
