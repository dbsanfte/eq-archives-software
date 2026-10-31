"""Bounded, private-network embedding throughput and vector compatibility checks."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import ipaddress
import json
import math
from pathlib import Path
import socket
import statistics
import struct
import threading
import time
from urllib.parse import urlsplit


def private_endpoint(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use an HTTP(S) endpoint without embedded credentials')
    addresses = {row[4][0] for row in socket.getaddrinfo(parsed.hostname, parsed.port or 80)}
    if not addresses or not all(ipaddress.ip_address(value).is_private for value in addresses):
        raise ValueError('Benchmarks require an explicitly private-network or loopback endpoint')
    return parsed


def vectors(payload, count, model):
    if payload.get('model') != model:
        raise ValueError('Embedding model alias changed')
    rows = payload.get('data')
    if not isinstance(rows, list) or len(rows) != count or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Incomplete embedding batch')
    indices = [row.get('index') for row in rows]
    if any(type(value) is not int for value in indices) or sorted(indices) != list(range(count)):
        raise ValueError('Invalid embedding indices')
    result = []
    for row in sorted(rows, key=lambda item: item['index']):
        values = row.get('embedding')
        if isinstance(values, str):
            raw = base64.b64decode(values, validate=True)
            if len(raw) != 768 * 4:
                raise ValueError('Invalid encoded embedding length')
            values = struct.unpack('<768f', raw)
        if (not isinstance(values, (list, tuple)) or len(values) != 768 or
                any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
            raise ValueError('Expected 768 finite dimensions')
        if abs(math.sqrt(sum(value * value for value in values)) - 1) > .001:
            raise ValueError('Embedding must be normalized')
        result.append(values)
    return result


class Client:
    def __init__(self, url, key, model, encoding='float'):
        self.url, self.key, self.model, self.encoding = private_endpoint(url), key, model, encoding
        self.local = threading.local()
        self.connections = []
        self.lock = threading.Lock()

    def embed(self, inputs):
        if not getattr(self.local, 'connection', None):
            cls = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
            self.local.connection = cls(self.url.hostname, self.url.port, timeout=60)
            with self.lock:
                self.connections.append(self.local.connection)
        connection = self.local.connection
        body = json.dumps({'model': self.model, 'input': inputs, 'encoding_format': self.encoding}).encode()
        connection.request('POST', self.url.path or '/', body,
                           {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.key})
        response = connection.getresponse()
        raw = response.read(16 * 1024 * 1024 + 1)
        if response.status != 200:
            raise ValueError(f'Embedding HTTP {response.status}; upstream body omitted')
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('Embedding response exceeds the benchmark byte bound')
        payload = json.loads(raw)
        if self.encoding == 'base64' and any(not isinstance(row.get('embedding'), str)
                                             for row in payload.get('data', [])):
            raise ValueError('Server did not honor the requested base64 encoding')
        return vectors(payload, len(inputs), self.model)

    def close(self):
        for connection in self.connections:
            connection.close()


def cosine(a, b):
    return sum(x*y for x, y in zip(a, b)) / math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))


def benchmark(client, inputs, *, seconds, concurrency, max_requests=10000):
    # Warmup is excluded; there is no paid endpoint or unbounded run mode.
    client.embed(inputs)
    start = time.perf_counter()
    deadline = start + seconds
    lock = threading.Lock()
    reserved = 0
    latencies = []
    def work():
        nonlocal reserved
        while time.perf_counter() < deadline:
            with lock:
                if reserved >= max_requests:
                    return
                reserved += 1
            then = time.perf_counter()
            client.embed(inputs)
            elapsed = time.perf_counter() - then
            with lock:
                latencies.append(elapsed)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(work) for _ in range(concurrency)]
        for future in futures:
            future.result()
    elapsed = time.perf_counter() - start
    if not latencies:
        raise ValueError('Benchmark made no requests')
    ordered = sorted(latencies)
    return {'seconds': elapsed, 'requests': len(latencies), 'batch_size': len(inputs),
            'concurrency': concurrency, 'inputs_per_second': len(latencies) * len(inputs) / elapsed,
            'characters_per_second': len(latencies) * sum(map(len, inputs)) / elapsed,
            'p50_seconds': statistics.median(ordered),
            'p95_seconds': ordered[min(len(ordered)-1, math.ceil(len(ordered)*.95)-1)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--api-key-file', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--fixtures', type=Path, required=True, help='Private JSON array of prefixed, token-checked inputs')
    parser.add_argument('--batch-size', type=int, default=2)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--seconds', type=float, default=15)
    parser.add_argument('--encoding', choices=['float', 'base64'], default='float')
    parser.add_argument('--reference-url', help='Optional existing private endpoint for serial-vector comparison')
    parser.add_argument('--min-cosine', type=float, default=.999,
                        help='Cross-backend tolerance; observed CUDA/Vulkan real chunks are >= .9995')
    args = parser.parse_args()
    if not .99 <= args.min_cosine <= 1:
        parser.error('Cosine threshold must be .99–1')
    if not 1 <= args.batch_size <= 256 or not 1 <= args.concurrency <= 16 or not 1 <= args.seconds <= 120:
        parser.error('Use batch 1–256, concurrency 1–16 and duration 1–120 seconds')
    if args.fixtures.stat().st_size > 8 * 1024 * 1024:
        parser.error('Fixture file exceeds 8 MiB')
    fixtures = json.loads(args.fixtures.read_text())
    if (not isinstance(fixtures, list) or not 1 <= len(fixtures) <= 256 or
            any(not isinstance(text, str) or len(text) > 20000 or not text.startswith('search_document: ') for text in fixtures)):
        parser.error('Fixtures must be prefixed document strings')
    key = args.api_key_file.read_text().strip()
    if not key:
        parser.error('The private API key file is empty')
    client = Client(args.url, key, args.model, args.encoding)
    try:
        compatibility = None
        if args.reference_url:
            reference = Client(args.reference_url, key, args.model)
            try:
                samples = fixtures[:8]
                expected = [reference.embed([text])[0] for text in samples]
                actual = client.embed(samples)
                compatibility = min(cosine(a, b) for a, b in zip(expected, actual, strict=True))
                if compatibility < args.min_cosine:
                    raise ValueError('CUDA/reference vectors fail the required cosine compatibility threshold')
            finally:
                reference.close()
        inputs = [fixtures[index % len(fixtures)] for index in range(args.batch_size)]
        result = benchmark(client, inputs, seconds=args.seconds, concurrency=args.concurrency)
        print(json.dumps({**result, 'encoding': args.encoding, 'reference_min_cosine': compatibility}))
    finally:
        client.close()


if __name__ == '__main__':
    main()
