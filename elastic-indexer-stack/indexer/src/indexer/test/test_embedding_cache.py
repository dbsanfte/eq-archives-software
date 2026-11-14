import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from indexer import gemma
from indexer.embedding_cache import EmbeddingCache,runtime_identity

V=[1.]+[0.]*767


class Time:
    value=1000.
    def __call__(self):return self.value


def cache(tmp_path,clock=None,**options):
    return EmbeddingCache(tmp_path/'cache.sqlite','pinned-model-and-runtime',clock=clock or Time(),**options)


def test_restart_reuses_exact_inputs_and_returns_independent_vectors(tmp_path):
    calls=[];a=cache(tmp_path)
    def fetch(inputs):calls.extend(inputs);return [V for _ in inputs]
    assert a.embed(['title: Raid | text: Author\nAdvice']*2,fetch)==[V,V]
    a.close();b=cache(tmp_path)
    result=b.embed(['title: Raid | text: Author\nAdvice']*2,fetch)
    assert len(calls)==1 and result[0] is not result[1]
    assert b.stats['hits']==1


def test_title_author_prefix_and_model_revision_are_separate_cache_identities(tmp_path):
    calls=[];a=cache(tmp_path)
    def fetch(inputs):calls.extend(inputs);return [V for _ in inputs]
    texts=['title: Raid | text: Author\nAdvice','title: Other | text: Author\nAdvice',
           'title: Raid | text: Guest\nAdvice','task: search result | query: Advice']
    a.embed(texts,fetch)
    b=EmbeddingCache(tmp_path/'cache.sqlite','different-model-revision',clock=Time())
    b.embed(texts,fetch)
    assert calls==texts*2


def test_heat_retains_frequently_used_inputs_over_more_recent_cold_inputs(tmp_path):
    clock=Time();a=cache(tmp_path,clock,max_entries=2,half_life=10,max_idle=1000)
    a.put_many(['hot'],[V])
    for _ in range(10):assert a.get_many(['hot'])=={'hot':V}
    clock.value+=9;a.put_many(['cold'],[V]);clock.value+=1;a.put_many(['new'],[V])
    assert a.get_many(['hot','cold','new'])=={'hot':V,'new':V}
    assert a.stats['evictions']==1


def test_heat_decays_so_an_old_hot_input_eventually_loses_to_recent_inputs(tmp_path):
    clock=Time();a=cache(tmp_path,clock,max_entries=2,half_life=10,max_idle=1000)
    a.put_many(['old-hot'],[V])
    for _ in range(10):a.get_many(['old-hot'])
    clock.value+=100;a.put_many(['recent'],[V]);a.put_many(['new'],[V])
    assert a.get_many(['old-hot','recent','new'])=={'recent':V,'new':V}


def test_idle_expiry_applies_to_hot_items_and_a_hit_refreshes_it(tmp_path):
    clock=Time();a=cache(tmp_path,clock,half_life=10,max_idle=20)
    a.put_many(['idle-hot','refreshed'],[V,V])
    for _ in range(20):a.get_many(['idle-hot'])
    clock.value+=18;a.get_many(['refreshed']);clock.value+=17
    assert a.get_many(['idle-hot','refreshed'])=={'refreshed':V}
    assert a.stats['expired']==1


@pytest.mark.parametrize('vectors',[[],[V,V],[[1.]*767],[[float('nan')]*768],[[0.]*768]])
def test_incomplete_or_invalid_inference_responses_never_enter_cache(tmp_path,vectors):
    a=cache(tmp_path)
    with pytest.raises(ValueError):a.embed(['source'],lambda inputs:vectors)
    assert a.get_many(['source'])=={}


def test_failed_later_vector_does_not_save_the_valid_first_vector(tmp_path):
    a=cache(tmp_path)
    with pytest.raises(ValueError):a.put_many(['a','b'],[V,[float('inf')]*768])
    assert a.get_many(['a','b'])=={}


def test_checksum_or_invalid_vector_is_evicted_and_refetched(tmp_path):
    a=cache(tmp_path);a.put_many(['source'],[V]);db=a.state.connection()
    db.execute('UPDATE embeddings SET vector=?',(b'corrupted',))
    calls=[]
    assert a.embed(['source'],lambda inputs:calls.append(inputs) or [V])==[V]
    assert calls==[['source']] and a.stats['corrupt']==1


def test_writer_contention_uses_inference_and_does_not_stall_the_pipeline(tmp_path):
    a=cache(tmp_path);a.put_many(['source'],[V])
    blocker=sqlite3.connect(tmp_path/'cache.sqlite');blocker.execute('BEGIN IMMEDIATE')
    try:
        calls=[];assert a.embed(['source'],lambda values:calls.append(values) or [V])==[V]
        assert calls==[['source']] and a.stats['unavailable']>=1
    finally:blocker.rollback();blocker.close()
    assert a.get_many(['source'])=={'source':V}


def test_cache_storage_and_entry_count_stay_bounded_under_concurrent_clients(tmp_path):
    start=threading.Barrier(4)
    def work(worker):
        a=cache(tmp_path,max_bytes=131072,max_entries=10);start.wait()
        for i in range(20):a.embed([f'{worker}-{i}'],lambda values:[V])
        a.close()
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(work,range(4)))
    with sqlite3.connect(tmp_path/'cache.sqlite') as db:
        assert db.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0]<=10
        assert db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]<=131072


def test_idle_cleanup_occurs_on_writes_and_freed_pages_are_reused(tmp_path):
    clock=Time();a=cache(tmp_path,clock,max_bytes=131072,max_entries=10,max_idle=10)
    for i in range(20):a.put_many([str(i)],[V])
    clock.value+=11;a.put_many(['new'],[V])
    assert a.state.connection().execute('SELECT COUNT(*) FROM embeddings').fetchone()[0]==1
    assert a.get_many(['new'])=={'new':V}


def test_runtime_identity_uses_actual_weight_runtime_and_tokenizer_pins(tmp_path,monkeypatch):
    path=tmp_path/'runtime.env';monkeypatch.setenv('EMBEDDING_CACHE_RUNTIME',str(path))
    cuda=tmp_path/'cuda.env';monkeypatch.setenv('EMBEDDING_CACHE_CUDA_RUNTIME',str(cuda))
    cuda.write_text('CUDA_IMAGE=cuda@sha256:'+'b'*64+'\n')
    path.write_text('MODEL_ALIAS='+gemma.MODEL+'\nMODEL_SHA256='+'a'*64+'\nLLAMA_IMAGE=runtime@sha256:'+'b'*64+'\n')
    first=runtime_identity();assert json.loads(first)['tokenizer']==gemma.TOKENIZER_SHA256
    assert json.loads(first)['runtime']=='cuda@sha256:'+'b'*64
    cuda.write_text('CUDA_IMAGE=cuda@sha256:'+'d'*64+'\n');assert runtime_identity()!=first
    legacy=runtime_identity()
    monkeypatch.setenv('EMBEDDING_CACHE_BACKEND_IMAGE','eqarchives-cuda-batch@sha256:'+'e'*64)
    assert runtime_identity()!=legacy and json.loads(runtime_identity())['runtime'].startswith('eqarchives-cuda-batch@')
    monkeypatch.setenv('EMBEDDING_CACHE_BACKEND_IMAGE','unversioned')
    with pytest.raises(ValueError):runtime_identity()
    monkeypatch.delenv('EMBEDDING_CACHE_BACKEND_IMAGE')
    first=runtime_identity()
    path.write_text(path.read_text().replace('a'*64,'c'*64));assert runtime_identity()!=first
    path.write_text('MODEL_ALIAS=wrong');
    with pytest.raises(ValueError):runtime_identity()


def test_environment_defaults_and_invalid_bounds_are_visible(tmp_path,monkeypatch):
    monkeypatch.delenv('EMBEDDING_CACHE_PATH',raising=False);assert EmbeddingCache.from_environment() is None
    for settings in ({'max_bytes':1},{'max_entries':0},{'half_life':0},{'max_idle':float('inf')}):
        with pytest.raises(ValueError):cache(tmp_path,**settings)
    a=cache(tmp_path,half_life=10);a.put_many(['source'],[V])
    b=cache(tmp_path,half_life=20)
    assert b.embed(['source'],lambda values:[V])==[V] and b.stats['unavailable']


def test_cache_does_not_follow_a_database_symlink(tmp_path):
    elsewhere=tmp_path/'elsewhere';elsewhere.write_text('private state')
    (tmp_path/'cache.sqlite').symlink_to(elsewhere)
    a=cache(tmp_path);assert a.embed(['source'],lambda values:[V])==[V]
    assert elsewhere.read_text()=='private state' and a.stats['unavailable']


def test_long_lived_reader_cannot_grow_the_journal_indefinitely(tmp_path,monkeypatch):
    import indexer.embedding_cache as module
    a=cache(tmp_path,max_bytes=131072,max_entries=10);a.put_many(['seed'],[V])
    a.state.connection().execute('PRAGMA wal_checkpoint(TRUNCATE)')
    reader=sqlite3.connect(tmp_path/'cache.sqlite');reader.execute('BEGIN');reader.execute('SELECT COUNT(*) FROM embeddings').fetchone()
    monkeypatch.setattr(module,'WAL_LIMIT',16384)
    try:
        for i in range(3):a.put_many([str(i)],[V])
        wal=tmp_path/'cache.sqlite-wal';size=wal.stat().st_size
        for i in range(3,6):a.put_many([str(i)],[V])
        assert wal.stat().st_size==size and a.stats['unavailable']
    finally:reader.rollback();reader.close()
    a.put_many(['after-reader'],[V]);assert a.get_many(['after-reader'])=={'after-reader':V}
