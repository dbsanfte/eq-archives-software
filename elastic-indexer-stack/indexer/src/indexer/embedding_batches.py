"""Bounded concurrent inference; cache and checkpoint work stays on the caller."""
from concurrent.futures import ThreadPoolExecutor, wait
import base64
import struct

from .gemma import vector


def decode_vectors(payload, count, model):
    if not isinstance(payload, dict) or payload.get('model') != model:
        raise ValueError('Embedding model identity differs')
    rows = payload.get('data')
    if (not isinstance(rows, list) or len(rows) != count
            or any(not isinstance(row, dict) or type(row.get('index')) is not int for row in rows)
            or sorted(row['index'] for row in rows) != list(range(count))):
        raise ValueError('Embedding response indices differ')
    result = []
    for row in sorted(rows, key=lambda row: row['index']):
        values = row.get('embedding')
        if isinstance(values, str):
            raw = base64.b64decode(values, validate=True)
            if len(raw) != 768 * 4:
                raise ValueError('Encoded embedding length differs')
            values = list(struct.unpack('<768f', raw))
        if not isinstance(values, list):
            raise ValueError('Embedding vector is not a list')
        result.append(vector(values).copy())
    return result


def embed_batches(inputs, infer, *, batch_size=8, workers=4, cache=None, check=lambda: None):
    if type(batch_size) is not int or not 1 <= batch_size <= 32:
        raise ValueError('Embedding batch size must be 1–32')
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('Embedding concurrency must be 1–4')
    if not inputs:
        return []
    result = []
    # Only inference runs in threads. SQLite state, ordered acknowledgements and
    # cooperative foreground/deadline checks remain on their original owner.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='embedding') as pool:
        def fetch(missing):
            check()
            futures = [pool.submit(infer, missing[i:i+batch_size])
                       for i in range(0, len(missing), batch_size)]
            try:
                values = []
                for i, future in enumerate(futures):
                    rows = future.result()
                    if len(rows) != len(missing[i*batch_size:(i+1)*batch_size]):
                        raise ValueError('Incomplete embedding batch')
                    values.extend(vector(row).copy() for row in rows)
                return values
            finally:
                # A failed batch must not release its reservation while sibling
                # requests still use the client; no partial document is returned.
                for future in futures:
                    future.cancel()
                wait(futures)
        for start in range(0, len(inputs), batch_size * workers):
            check()
            wave = inputs[start:start + batch_size * workers]
            values = cache.embed(wave, fetch) if cache else fetch(wave)
            if len(values) != len(wave):
                raise ValueError('Incomplete embedding wave')
            result.extend(vector(row).copy() for row in values)
    return result
