"""Bounded operator capacity probe using private source-linked search fixtures.

Run inside the model container with FIXTURE injected by the operator, or pass
--fixtures/--url/--key-file. Outputs timings and counts only, never source text.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import statistics
import threading
import time
import urllib.request

EMBED = 'embeddinggemma-2-768-v1'
RERANK = 'qwen3-reranker-0.6b-q8-v1'


def cases(fixture):
    rows = []
    for item in fixture[:8]:
        documents = []
        for identity in item['ids'][:60]:
            evidence = item['evidence'].get(identity, {})
            passage = evidence.get('passages', {}).get('semantic') or evidence.get('passages', {}).get('lexical')
            if passage and isinstance(passage.get('text_chunk'), str):
                documents.append('\n'.join(str(passage.get(k) or '') for k in ('title', 'context', 'text_chunk'))[:900])
        if len(documents) >= 20:
            rows.append((item['query'], documents))
    if len(rows) < 2:
        raise ValueError('Need two source-linked search cases')
    return rows


def probe(fixture, url, embed_key, rerank_key, levels=(1, 2, 4, 8)):
    inputs = cases(fixture)
    slots = threading.BoundedSemaphore(8)
    def post(path, value, key):
        with slots:
            request = urllib.request.Request(url+path, data=json.dumps(value).encode(),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+key})
            with urllib.request.urlopen(request, timeout=10) as response:
                data = response.read(65537)
            if len(data) > 65536:
                raise ValueError('Oversized inference response')
            return json.loads(data)
    results = []
    for level in levels:
        barrier = threading.Barrier(level)
        def search(n):
            query, docs = inputs[n % len(inputs)]
            barrier.wait(timeout=5)
            start = time.monotonic()
            try:
                data = post('/v1/embeddings', {'model': EMBED, 'input': ['task: search result | query: '+query]}, embed_key)
                vectors = data['data']
                if len(vectors) != 1 or len(vectors[0]['embedding']) != 768 or not all(math.isfinite(x) for x in vectors[0]['embedding']):
                    raise ValueError('Invalid embedding')
                def rank(offset):
                    batch = docs[offset:offset+20]
                    data = post('/v1/rerank', {'model': RERANK, 'query': query, 'documents': batch}, rerank_key)
                    scores = sorted(data['results'], key=lambda row: row['index'])
                    if ([r['index'] for r in scores] != list(range(len(batch))) or
                            any(not math.isfinite(r['relevance_score']) or not 0 <= r['relevance_score'] <= 1 for r in scores)):
                        raise ValueError('Invalid scores')
                with ThreadPoolExecutor(max_workers=3) as pool:
                    list(pool.map(rank, range(0, len(docs), 20)))
                return {'seconds': time.monotonic()-start, 'documents': len(docs), 'ok': True}
            except Exception as error:
                return {'seconds': time.monotonic()-start, 'ok': False, 'category': type(error).__name__,
                        'http_status': getattr(error, 'code', None)}
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=level) as pool:
            observations = list(pool.map(search, range(level)))
        elapsed = time.monotonic()-start
        timings = sorted(row['seconds'] for row in observations)
        row = {'concurrent_searches': level, 'seconds': elapsed,
            'successes': sum(r['ok'] for r in observations),
            'p50_seconds': statistics.median(timings), 'max_seconds': max(timings),
            'searches_per_second': sum(r['ok'] for r in observations)/elapsed,
            'observations': observations}
        results.append(row)
        print(json.dumps(row), flush=True)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path)
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    parser.add_argument('--key-file', type=Path, default=Path('/run/secrets/embed_key'))
    parser.add_argument('--query-key', action='store_true')
    args = parser.parse_args()
    fixture = globals().get('FIXTURE') or json.loads(args.fixtures.read_text())
    key = Path('/run/secrets/rerank_key').read_text().strip()
    probe(fixture, args.url, key if args.query_key else args.key_file.read_text().strip(), key)
