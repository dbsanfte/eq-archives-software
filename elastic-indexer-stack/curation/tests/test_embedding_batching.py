import base64
from concurrent.futures import ThreadPoolExecutor
import struct
import threading

import pytest

from indexer.embedding_batches import decode_vectors, embed_batches
from indexer.embedding_cache import EmbeddingCache
from indexer.gemma import MODEL

V = [1.] + [0.] * 767


def test_four_workers_overlap_and_restore_all_input_positions_with_partial_tail():
    lock, barrier = threading.Lock(), threading.Barrier(4)
    active = peak = 0
    calls = []
    def infer(rows):
        nonlocal active, peak
        with lock:
            calls.append(rows)
            active += 1
            peak = max(peak, active)
        try:
            if len(rows) == 8:
                barrier.wait(timeout=3)
            return [[int(row) + 1.] + [0.] * 767 for row in rows]
        finally:
            with lock:
                active -= 1
    result = embed_batches([str(i) for i in range(35)], infer)
    assert [v[0] for v in result] == list(range(1, 36))
    assert peak == 4 and active == 0
    assert sorted(map(len, calls)) == [3, 8, 8, 8, 8]


def test_failure_drains_siblings_without_writing_cache_or_starting_later_waves(tmp_path):
    started, release, failed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    cache = EmbeddingCache(tmp_path/'cache.sqlite', 'batch-test')
    def infer(rows):
        calls.append(rows)
        if rows == ['bad']:
            assert started.wait(3)
            failed.set()
            raise ValueError('failed batch')
        started.set()
        assert release.wait(3)
        return [V]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(embed_batches, ['bad', 'slow', 'later'], infer,
                             batch_size=1, workers=2, cache=cache)
        try:
            assert failed.wait(3)
            assert not future.done()
        finally:
            release.set()
        with pytest.raises(ValueError, match='failed batch'):
            future.result(timeout=3)
    assert sorted(calls) == [['bad'], ['slow']]
    assert cache.get_many(['bad', 'slow']) == {}
    cache.close()


def test_cache_and_yield_checks_stay_on_caller_and_completed_waves_survive(tmp_path):
    owner = threading.get_ident()
    cache = EmbeddingCache(tmp_path/'cache.sqlite', 'batch-test')
    original = cache.embed
    def cached(rows, infer):
        assert threading.get_ident() == owner
        return original(rows, infer)
    cache.embed = cached
    calls, checks = [], []
    def infer(rows):
        assert threading.get_ident() != owner
        calls.append(rows)
        return [V for _ in rows]
    def check():
        assert threading.get_ident() == owner
        checks.append(True)
        if len(checks) == 3:
            raise InterruptedError('foreground import')
    with pytest.raises(InterruptedError):
        embed_batches(['a', 'b', 'c'], infer, batch_size=1, workers=2, cache=cache, check=check)
    assert cache.get_many(['a', 'b', 'c']) == {'a': V, 'b': V}
    assert embed_batches(['a', 'b', 'c'], infer, batch_size=1, workers=2, cache=cache) == [V]*3
    assert sorted(calls) == [['a'], ['b'], ['c']]
    cache.close()


@pytest.mark.parametrize('bad', [[], [[0.]*768], [[float('nan')]*768], [[1.]*767]])
def test_incomplete_or_invalid_batch_never_enters_cache(tmp_path, bad):
    cache = EmbeddingCache(tmp_path/'cache.sqlite', 'batch-test')
    with pytest.raises(ValueError):
        embed_batches(['a'], lambda rows: bad, cache=cache)
    assert cache.get_many(['a']) == {}
    cache.close()


def test_empty_work_and_configuration_bounds():
    assert embed_batches([], lambda rows: pytest.fail('No request for empty work')) == []
    for kwargs in ({'batch_size':0}, {'batch_size':33}, {'batch_size':True},
                   {'workers':0}, {'workers':5}, {'workers':True}):
        with pytest.raises(ValueError):
            embed_batches(['a'], lambda rows:[V], **kwargs)


def test_wire_decoding_preserves_indices_and_rejects_partial_nonfinite_or_wrong_model():
    encoded = base64.b64encode(struct.pack('<768f', *V)).decode()
    payload = {'model':MODEL,'data':[{'index':1,'embedding':encoded},{'index':0,'embedding':V}]}
    assert decode_vectors(payload, 2, MODEL) == [V, V]
    invalid = [None, {}, dict(payload,model='wrong'), dict(payload,data=[]),
               dict(payload,data=[{'index':True,'embedding':V}]),
               dict(payload,data=[{'index':0,'embedding':None}]),
               dict(payload,data=[{'index':0,'embedding':'invalid!'}]),
               dict(payload,data=[{'index':0,'embedding':base64.b64encode(b'x').decode()}]),
               dict(payload,data=[{'index':0,'embedding':[float('inf')]*768}])]
    for value in invalid:
        with pytest.raises(ValueError):
            decode_vectors(value, 1, MODEL)
