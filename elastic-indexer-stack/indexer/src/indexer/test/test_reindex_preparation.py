from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from indexer.chunking import SourceChunk, chunk_source
from indexer.reindex_preparation import prepare_source, verify_prepared
import reindex_text as job
from indexer.test.test_reindex_text import setup_record


def test_spawned_preparation_reads_complete_source_without_network_or_writes(tmp_path):
    path = tmp_path/'source.html'
    path.write_text('<p>Cleric complete heal and guild raids.</p>' * 300)
    original = path.read_bytes()
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn')) as pool:
        text, chunks = pool.submit(prepare_source, 'websites/example.org/20000101000000/index.html',
                                   str(path), True, 'old text').result(timeout=30)
    assert 'Cleric complete heal' in text
    assert chunks == chunk_source(text)
    verify_prepared(text, chunks)
    assert path.read_bytes() == original


def test_missing_source_preserves_original_and_preparation_failures_are_sanitized(tmp_path):
    text, chunks = prepare_source('websites/x', str(tmp_path/'missing'), False, 'preserved original')
    assert text == 'preserved original' and chunks == chunk_source(text)
    with pytest.raises(ValueError, match='Source preparation failed') as error:
        prepare_source('websites/x', str(tmp_path/'private-source-path'), True, '')
    assert 'private-source-path' not in str(error.value)


@pytest.mark.parametrize('text,chunks', [
    ('cleric', []), ('cleric', [SourceChunk(1,6,'leric')]),
    ('cleric', [SourceChunk(0,5,'cleri')]), ('cleric', [SourceChunk(0,6,'wrong')]),
    ('cleric', [SourceChunk(0,7,'cleric')]),
    ('cleric', [SourceChunk(0,2,'cl'), SourceChunk(3,6,'ric')]),
    ('cleric', [SourceChunk(0,4,'cler'), SourceChunk(0,6,'cleric')]),
])
def test_prepared_chunks_cannot_change_or_omit_source(text,chunks):
    with pytest.raises(ValueError): verify_prepared(text,chunks)


def test_prepared_source_stays_bound_to_one_exact_record_and_checkpoint(tmp_path):
    hit,client,handler,embedder=setup_record(tmp_path)
    chunks=chunk_source('complete source')
    pool=Mock()
    pool.submit.return_value.result.return_value=('complete source',chunks)
    progress=job.Progress()
    job.reindex_record(client,'eq-archive',hit,tmp_path,handler,embedder,progress,pool)
    assert pool.submit.call_args.args[1:]==(hit['_id'],str(tmp_path/hit['_id']),True,'opening paragraph')
    handler._get_documents_from_file.assert_not_called()
    assert embedder.get_chunks_and_embeddings.call_args.kwargs['_prepared_source']==chunks
    assert client.update.call_args.kwargs['if_seq_no']==17
    assert progress.updated==1


def test_bad_prepared_data_never_embeds_or_updates(tmp_path):
    hit,client,handler,embedder=setup_record(tmp_path)
    pool=Mock();pool.submit.return_value.result.return_value=('complete source',[])
    with pytest.raises(ValueError):
        job.reindex_record(client,'eq-archive',hit,tmp_path,handler,embedder,job.Progress(),pool)
    embedder.get_chunks_and_embeddings.assert_not_called();client.update.assert_not_called()


def test_empty_prepared_extraction_cannot_replace_existing_text(tmp_path):
    hit,client,handler,embedder=setup_record(tmp_path)
    pool=Mock();pool.submit.return_value.result.return_value=('',[])
    with pytest.raises(ValueError,match='empty extraction'):
        job.reindex_record(client,'eq-archive',hit,tmp_path,handler,embedder,job.Progress(),pool)
    client.update.assert_not_called()


def test_invalid_preparation_worker_count_rejected_before_clients(monkeypatch):
    monkeypatch.setattr('sys.argv',['reindex','--preparation-workers','5'])
    with pytest.raises(SystemExit): job.main()


def test_dead_preparation_process_stops_scan_for_checkpointed_job_retry(tmp_path):
    from concurrent.futures.process import BrokenProcessPool
    hit,client,handler,embedder=setup_record(tmp_path)
    client.count.return_value={'count':1000000}
    client.search.return_value={'hits':{'hits':[hit]*100}}
    pool=Mock();pool.submit.side_effect=BrokenProcessPool('worker exited')
    with pytest.raises(RuntimeError,match='checkpoints retained'):
        job.reindex(client,'eq-archive',tmp_path,handler,embedder,preparation_pool=pool)
    assert pool.submit.call_count==1
    client.update.assert_not_called()
