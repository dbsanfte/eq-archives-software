from concurrent.futures import Future
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from openai import APIConnectionError
import httpx

import reindex_embeddings as job
from indexer import gemma
from indexer.html_extraction import extraction_version

V = [1.] + [0.] * 767


def fixture():
    rid = 'websites/example.org/20000101000000/index.html'
    source = dict(id=rid, file_type='text', text_full='Original complete text.',
                  text_extraction_version=extraction_version(rid), text_chunking_version='old-nomic',
                  llm_summary='Existing summary', llm_image_text=['Cleric', 'raid'],
                  llm_tags=['Guild'], archive_source_sha256='original', last_indexed='2000-01-01')
    hit = dict(_id=rid, _source=source, _seq_no=17, _primary_term=2, sort=[rid])
    client = MagicMock()
    client.update.return_value = {'result': 'updated'}
    client.count.return_value = {'count': 1}
    embedder = MagicMock()
    embedder.get_chunks_and_embeddings.side_effect = lambda document, **kw: [
        {'text_chunk': c.text, 'vector': np.asarray(V)} for c in kw['_prepared_source']]
    embedder.embed_text.return_value = np.asarray(V)
    return hit, client, embedder


def test_atomic_replacement_of_all_searched_vectors_preserves_all_enrichment_and_dates(tmp_path):
    hit, client, embedder = fixture()
    before = deepcopy(hit['_source'])
    assert job.migrate_record(client, 'eq-archive', hit, tmp_path, embedder) == (1, 0)
    args = client.update.call_args.kwargs
    assert (args['if_seq_no'], args['if_primary_term']) == (17, 2)
    patch = args['doc']
    assert patch['embedding_model'] == gemma.MODEL
    assert patch['llm_summary_vector'] == patch['llm_image_text_vector'] == V
    assert patch['text'][0]['text_chunk'] == before['text_full']
    for field in ('llm_summary', 'llm_image_text', 'llm_tags', 'archive_source_sha256', 'last_indexed'):
        assert field not in patch
    embedder.embed_text.assert_any_call('Cleric raid')
    assert hit['_source'] == before
    hit['_source']['embedding_model'] = gemma.MODEL
    client.reset_mock()
    assert job.migrate_record(client, 'eq-archive', hit, tmp_path, embedder) == (0, 0)
    client.update.assert_not_called()


def test_bad_summary_or_id_cannot_publish_partial_model_checkpoint(tmp_path):
    hit, client, embedder = fixture()
    embedder.embed_text.side_effect = [np.asarray(V), np.asarray([float('nan')]*768)]
    with pytest.raises(ValueError):
        job.migrate_record(client, 'eq-archive', hit, tmp_path, embedder)
    client.update.assert_not_called()
    hit['_source']['id'] = 'other'
    with pytest.raises(ValueError, match='ID'):
        job.migrate_record(client, 'eq-archive', hit, tmp_path, embedder)


def test_missing_original_uses_stored_chunks_and_clears_unbacked_old_vectors(tmp_path):
    hit, client, embedder = fixture()
    hit['_source'].update(text_full='', text=[{'text_chunk': 'Preserved chunk'}],
                          text_extraction_version='old', llm_summary=None, llm_image_text=None)
    assert job.migrate_record(client, 'eq-archive', hit, tmp_path, embedder) == (1, 1)
    patch = client.update.call_args.kwargs['doc']
    assert patch['text'][0]['text_chunk'] == 'Preserved chunk'
    assert patch['llm_summary_vector'] is patch['llm_image_text_vector'] is None
    assert 'text_full' not in patch and 'text_extraction_version' not in patch


def test_old_extraction_repaired_without_repeating_source_preparation(tmp_path, monkeypatch):
    hit, client, embedder = fixture()
    source = hit['_source']
    source['text_extraction_version'] = 'old'
    path = tmp_path / source['id']
    path.parent.mkdir(parents=True)
    path.write_text('<p>A complete cleric guide</p>')
    groups, patch, missing = job.prepare(source, tmp_path)
    assert 'A complete cleric guide' in patch['text_full'] and not missing
    future = Future()
    future.set_result((groups, patch, missing))
    pool = MagicMock()
    pool.submit.return_value = future
    job.migrate_record(client, 'eq-archive', hit, tmp_path, embedder, pool)
    assert client.update.call_args.kwargs['doc']['text_extraction_version'] == extraction_version(source['id'])
    reader = MagicMock()
    reader._get_documents_from_file.return_value = []
    monkeypatch.setattr(job, 'handler', lambda: reader)
    with pytest.raises(ValueError, match='incomplete'):
        job.prepare(source, tmp_path)


def test_scan_only_skips_new_model_complete_checkpoints_and_limits_work(tmp_path):
    hit, client, embedder = fixture()
    client.search.side_effect = [{'hits': {'hits': [hit]}}, {'hits': {'hits': []}}]
    factory = MagicMock(return_value=embedder)
    result = job.reindex(client, 'eq', tmp_path, ids=[hit['_id']], limit=1, workers=1, batch_size=1, factory=factory)
    assert result.updated == result.processed == 1 and not result.failed
    query = client.count.call_args.kwargs['query']
    assert query['bool']['must_not'] == [{'term': {'embedding_model': gemma.MODEL}}]
    assert {'ids': {'values': [hit['_id']]}} in query['bool']['filter']
    assert client.search.call_args.kwargs['seq_no_primary_term']


def test_scan_advances_and_content_errors_do_not_block_later_records(tmp_path):
    hit, client, embedder = fixture()
    bad = deepcopy(hit)
    bad['_source']['id'] = 'bad'
    client.search.side_effect = [{'hits': {'hits': [bad]}}, {'hits': {'hits': [hit]}}, {'hits': {'hits': []}}]
    result = job.reindex(client, 'eq', tmp_path, workers=2, batch_size=1, factory=lambda **kw: embedder)
    assert result.failed == result.updated == 1 and result.processed == 2
    assert client.search.call_args_list[1].kwargs['search_after'] == hit['sort']


def test_transient_outage_exits_for_recovery_instead_of_finishing_incomplete(tmp_path):
    hit, client, embedder = fixture()
    client.search.side_effect = [{'hits': {'hits': [deepcopy(hit) for _ in range(8)]}}, {'hits': {'hits': []}}]
    embedder.embed_text.side_effect = APIConnectionError(request=httpx.Request('POST', 'http://fixture'))
    with pytest.raises(RuntimeError, match='Repeated upstream') as caught:
        job.reindex(client, 'eq', tmp_path, workers=1, factory=lambda **kw: embedder)
    assert isinstance(caught.value.__context__, APIConnectionError)
    client.update.assert_not_called()


def test_bad_bounds_and_partial_scan_fail_visibly(tmp_path):
    hit, client, embedder = fixture()
    with pytest.raises(ValueError, match='bounds'):
        job.reindex(client, 'eq', tmp_path, workers=0)
    client.search.return_value = {'timed_out': True}
    with pytest.raises(ValueError, match='Incomplete'):
        job.reindex(client, 'eq', tmp_path)


@pytest.mark.parametrize('failure', ['chunks', 'summary', 'update'])
def test_incomplete_record_cannot_be_marked_ready(tmp_path, failure):
    hit, client, embedder = fixture()
    if failure == 'chunks':
        embedder.get_chunks_and_embeddings.side_effect = lambda *a, **kw: []
    if failure == 'summary':
        embedder.embed_text.return_value = None
    if failure == 'update':
        client.update.return_value = {'result': 'failed'}
    with pytest.raises(ValueError):
        job.migrate_record(client, 'eq', hit, tmp_path, embedder)
    if failure != 'update':
        client.update.assert_not_called()


@pytest.mark.parametrize('failures', [0, 1])
def test_operator_entrypoint_only_adds_mapping_and_runs_checkpointed_migration(tmp_path, monkeypatch, failures):
    monkeypatch.setattr('sys.argv', ['reindex_embeddings', '--repo', str(tmp_path), '--preparation-workers', '0', '--limit', '1'])
    manager = MagicMock()
    manager._index_name = 'eq'
    monkeypatch.setattr(job, 'ElasticsearchManager', lambda **kw: manager)
    pool = MagicMock()
    monkeypatch.setattr(job, 'ProcessPoolExecutor', pool)
    migrate = MagicMock(return_value=job.Progress(failed=failures))
    monkeypatch.setattr(job, 'reindex', migrate)
    assert job.main() == failures
    manager.get_client.return_value.indices.put_mapping.assert_called_once_with(
        index='eq', properties={'embedding_model': {'type': 'keyword'}})
    assert migrate.call_args.kwargs['preparation_pool'] is None
    assert migrate.call_args.kwargs['limit'] == 1
    monkeypatch.setattr('sys.argv', ['reindex_embeddings', '--preparation-workers', '5'])
    with pytest.raises(SystemExit):
        job.main()
