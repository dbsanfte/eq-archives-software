"""Measure two concurrent searches under continuous four-request embedding load."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

from backend import EMBED_MODEL, RERANK_MODEL
from benchmark_http import checked_values, load_cases, rerank_batches


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fixtures', type=Path, required=True)
    p.add_argument('--root', type=Path, default=Path('/models'))
    p.add_argument('--url', default='http://127.0.0.1:8084')
    p.add_argument('--embed-key', type=Path, default=Path('/run/secrets/embed_key'))
    p.add_argument('--rerank-key', type=Path, default=Path('/run/secrets/rerank_key'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--rounds', type=int, default=12)
    args = p.parse_args()
    if not 6 <= args.rounds <= 24:
        p.error('Use 6–24 bounded rounds')
    cases = load_cases(args.fixtures, args.root)
    inputs = list(dict.fromkeys(row for case in cases for row in case['embeddings']))[:128]
    if len(cases) < 2 or len(inputs) < 32:
        p.error('Require two real query cases and at least 32 unique embedding inputs')
    import httpx

    stop, slots = threading.Event(), threading.BoundedSemaphore(4)
    counts, failures = [0] * 4, []
    report = {'rounds': [], 'unique_embedding_inputs': len(inputs)}
    clients = {kind: httpx.Client(base_url=args.url, trust_env=False, timeout=15,
               headers={'Authorization': 'Bearer ' + key.read_text().strip()})
               for kind, key in [('embed', args.embed_key), ('rerank', args.rerank_key)]}

    def background(worker):
        position = worker * 8
        try:
            while not stop.is_set():
                rows = inputs[position:position+8]
                position = (position+32) % len(inputs)
                reply = clients['embed'].post('/v1/embeddings', json={
                    'model': EMBED_MODEL, 'input': rows, 'encoding_format': 'float'})
                reply.raise_for_status()
                checked_values('embed', reply.json(), len(rows))
                counts[worker] += len(rows)
        except Exception as error:
            failures.append(type(error).__name__)

    def search(case):
        start = time.perf_counter()
        documents = list(dict.fromkeys(case['documents'][:60]))
        def batch(rows):
            with slots:
                reply = clients['rerank'].post('/v1/rerank', json={
                    'model': RERANK_MODEL, 'query': case['query'], 'documents': rows})
                reply.raise_for_status()
                return checked_values('rerank', reply.json(), len(rows))
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = [v for row in pool.map(batch, rerank_batches(case['query'], documents)) for v in row]
        if len(values) != len(documents):
            raise ValueError('Incomplete mixed-load scoring')
        return {'seconds': time.perf_counter()-start, 'documents': len(documents)}

    start = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=4) as background_pool:
            tasks = [background_pool.submit(background, i) for i in range(4)]
            try:
                for i in range(args.rounds):
                    with ThreadPoolExecutor(max_workers=2) as searches:
                        pair = list(searches.map(search, [cases[i % len(cases)], cases[(i+1) % len(cases)]]))
                    report['rounds'].append(pair)
                    print(json.dumps({'round': i, 'searches': pair}), flush=True)
            finally:
                stop.set()
                for task in tasks:
                    task.result()
    except Exception as error:
        failures.append(type(error).__name__)
    finally:
        for client in clients.values():
            client.close()
    report.update(errors=failures, embedding_inputs=sum(counts),
                  embedding_inputs_per_second=sum(counts)/(time.perf_counter()-start),
                  all_searches_under_3_seconds=(not failures and len(report['rounds']) == args.rounds
                      and all(row['seconds'] < 3 for pair in report['rounds'] for row in pair)))
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    return 0 if report['all_searches_under_3_seconds'] and all(counts) else 1


if __name__ == '__main__':
    raise SystemExit(main())
