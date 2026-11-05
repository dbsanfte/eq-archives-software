from concurrent.futures import Future
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from openai import APIConnectionError
import httpx

import reindex_embeddings as job
from indexer import gemma, passages
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
        {'text_chunk': c.text, 'vector': np.asarray(V)} for c in kw['_prepared_passages'].passages]
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
    assert patch['text'][0]['text_chunk'].strip() == before['text_full'].strip()
    for field in ('llm_summary', 'llm_image_text', 'llm_tags', 'archive_source_sha256', 'last_indexed'):
        assert field not in patch
    embedder.embed_text.assert_any_call('Cleric raid')
    assert hit['_source'] == before
    hit['_source'].update(embedding_model=gemma.MODEL, text_chunking_version=passages.VERSION)
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


def test_existing_model_checkpoint_is_reprocessed_when_its_passage_policy_is_old(tmp_path):
    hit,client,embedder=fixture()
    hit['_source']['embedding_model']=gemma.MODEL
    assert job.migrate_record(client,'eq-archive',hit,tmp_path,embedder)[0]==1
    assert client.update.call_args.kwargs['doc']['text_chunking_version']==passages.VERSION


def test_placeholder_summary_is_cleared_without_an_inference_call(tmp_path):
    hit,client,embedder=fixture()
    hit['_source'].update(llm_summary='[ Still awaiting LLM Enrichment... ]',llm_image_text=None)
    job.migrate_record(client,'eq-archive',hit,tmp_path,embedder)
    embedder.embed_text.assert_not_called()
    assert client.update.call_args.kwargs['doc']['llm_summary_vector'] is None


def test_binary_document_keeps_its_existing_extracted_text_and_extraction_checkpoint(tmp_path):
    hit,client,embedder=fixture()
    hit['_source'].update(mime_type='application/pdf',text_extraction_version='docling-pdf-v1')
    path=tmp_path/hit['_id'];path.parent.mkdir(parents=True);path.write_bytes(b'%PDF-1.7 binary source')
    job.migrate_record(client,'eq-archive',hit,tmp_path,embedder)
    patch=client.update.call_args.kwargs['doc']
    assert 'text_full' not in patch and 'text_extraction_version' not in patch
    assert patch['text'][0]['text_chunk'].strip()==hit['_source']['text_full']


def test_archived_error_clears_all_semantic_vectors_and_preserves_the_reader_and_enrichment(tmp_path):
    hit,client,embedder=fixture()
    source=hit['_source']
    raw='<title>System Message: Error</title><h1>Error</h1><p>The post you selected no longer exists.</p>'
    path=tmp_path/source['id'];path.parent.mkdir(parents=True);path.write_text(raw)
    source.update(text_full='Original error reader',mime_type='text/html',llm_summary='Historical summary retained')
    diagnostics={}
    job.migrate_record(client,'eq-archive',hit,tmp_path,embedder,diagnostics=diagnostics)
    patch=client.update.call_args.kwargs['doc']
    assert patch['text']==[]
    assert patch['llm_summary_vector'] is None and patch['llm_image_text_vector'] is None
    assert 'text_full' not in patch and 'llm_summary' not in patch
    assert patch['text_chunking_version']==passages.VERSION
    embedder.embed_text.assert_not_called()
    assert diagnostics['excluded_documents']==1 and diagnostics['passages']==0


def test_pipeline_quality_counters_record_only_acknowledged_writes(tmp_path):
    hit,client,embedder=fixture();diagnostics={}
    job.migrate_record(client,'eq-archive',hit,tmp_path,embedder,diagnostics=diagnostics)
    assert diagnostics['passages']==1
    assert diagnostics['excluded_documents']==0
    client.update.return_value={'result':'unexpected'};failed={}
    with pytest.raises(ValueError,match='did not update'):
        job.migrate_record(client,'eq-archive',hit,tmp_path,embedder,diagnostics=failed)
    assert failed=={}


def test_missing_original_uses_stored_chunks_and_clears_unbacked_old_vectors(tmp_path):
    hit, client, embedder = fixture()
    hit['_source'].update(text_full='', text=[{'text_chunk': 'Preserved chunk'}],
                          text_extraction_version='old', llm_summary=None, llm_image_text=None)
    assert job.migrate_record(client, 'eq-archive', hit, tmp_path, embedder) == (1, 1)
    patch = client.update.call_args.kwargs['doc']
    assert patch['text'][0]['text_chunk'].strip() == 'Preserved chunk'
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
    completed=query['bool']['must_not'][0]['bool']['filter']
    assert {'term':{'embedding_model':gemma.MODEL}} in completed
    assert {'term':{'text_chunking_version':passages.VERSION}} in completed
    assert completed[-1]['bool']['minimum_should_match']==1
    assert {'ids': {'values': [hit['_id']]}} in query['bool']['filter']
    assert client.search.call_args.kwargs['seq_no_primary_term']


def test_completed_gemma_record_with_bad_japanese_reader_repairs_atomically_without_enrichment(tmp_path):
    hit,client,embedder=fixture();source=hit['_source']
    text='<html><title>冒険記録</title><p>こんにちは。みんなで①番の髙い山に行きましょう。</p></html>'
    raw=text.encode('cp932');path=tmp_path/hit['_id'];path.parent.mkdir(parents=True);path.write_bytes(raw)
    source.update(embedding_model=gemma.MODEL,text_chunking_version=passages.VERSION,
                  text_extraction_version='html-2026-10-06-source-links',text_full=raw.decode('latin-1'),mime_type='text/html',
                  title='冒険記録'.encode('cp932').decode('latin-1'))
    assert not job.current_source(source)
    assert job.migrate_record(client,'eq',hit,tmp_path,embedder)==(1,0)
    patch=client.update.call_args.kwargs['doc']
    assert 'こんにちは。みんなで①番の髙い山に行きましょう。' in patch['text_full']
    assert any('こんにちは' in c['text_chunk'] for c in patch['text'])
    assert patch['text_passage_provenance']['decoding']['encoding']=='cp932'
    assert patch['text_extraction_version']==extraction_version(hit['_id'])
    assert 'llm_summary' not in patch and 'last_indexed' not in patch and patch['title']=='冒険記録'
    assert client.update.call_args.kwargs['if_seq_no']==17


def test_clean_completed_record_reuses_vectors_when_only_decoding_checkpoint_is_new(tmp_path):
    hit,client,embedder=fixture();source=hit['_source'];path=tmp_path/hit['_id'];path.parent.mkdir(parents=True)
    path.write_text('<html><title>Guild</title><p>A complete historical guide.</p></html>')
    source.update(mime_type='text/html',text_extraction_version='old')
    groups,patch,_=job.prepare(source,tmp_path)
    source.update(text_full=patch['text_full'],text_passage_provenance={'version':passages.VERSION,'sources':[p.provenance() for _,p in groups]},
                  embedding_model=gemma.MODEL,text_chunking_version=passages.VERSION)
    source['text']=[{'text_chunk':p.text,'title':p.title,'context':p.context,'kind':p.kind,'source_start':p.start,'source_end':p.end} for _,plan in groups for p in plan.passages]
    diagnostics={}
    assert job.migrate_record(client,'eq',hit,tmp_path,embedder,diagnostics=diagnostics)==(1,0)
    update=client.update.call_args.kwargs['doc']
    assert diagnostics=={'reused_embeddings':1}
    assert 'text' not in update and 'llm_summary_vector' not in update
    assert update['text_extraction_version']==extraction_version(source['id'])
    assert update['text_passage_provenance']['decoding']['version']=='source-decoding-v2' and 'llm_image_text_vector' not in update
    embedder.get_chunks_and_embeddings.assert_not_called();embedder.embed_text.assert_not_called()
    client.update.return_value={'result':'unexpected'}
    with pytest.raises(ValueError,match='did not update'):job.migrate_record(client,'eq',hit,tmp_path,embedder)


@pytest.mark.parametrize('prefix,mime,filetype',[
    ('newsgroups/','text/plain','text'),('mailing-lists/','application/json','text'),
    ('websites/','application/pdf','text'),('websites/','image/gif','image')])
def test_decoding_checkpoint_keeps_unaffected_source_types_complete(prefix,mime,filetype):
    assert job.current_source({'id':prefix+'example','mime_type':mime,'file_type':filetype})


def test_scan_advances_and_content_errors_do_not_block_later_records(tmp_path):
    hit, client, embedder = fixture()
    bad = deepcopy(hit)
    bad['_source']['id'] = 'bad'
    client.search.side_effect = [{'hits': {'hits': [bad]}}, {'hits': {'hits': [hit]}}, {'hits': {'hits': []}}]
    result = job.reindex(client, 'eq', tmp_path, workers=2, batch_size=1, factory=lambda **kw: embedder)
    assert result.failed == result.updated == 1 and result.processed == 2
    assert client.search.call_args_list[1].kwargs['search_after'] == hit['sort']


def test_handoff_finishes_unconverted_records_before_upgrading_existing_gemma_passages(tmp_path):
    hit,client,embedder=fixture()
    old=deepcopy(hit);old['_source']['embedding_model']=gemma.MODEL
    client.search.side_effect=[{'hits':{'hits':[hit]}},{'hits':{'hits':[]}},
                               {'hits':{'hits':[old]}},{'hits':{'hits':[]}}]
    result=job.reindex(client,'eq',tmp_path,workers=1,factory=lambda **_:embedder,prioritize_unconverted=True)
    assert result.updated==2
    queries=[call.kwargs['query'] for call in client.search.call_args_list]
    assert {'term':{'embedding_model':gemma.MODEL}} in queries[0]['bool']['must_not']
    assert {'term':{'embedding_model':gemma.MODEL}} in queries[-1]['bool']['filter']


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
        index='eq', properties={'embedding_model': {'type': 'keyword'}, 'text_chunking_version': {'type': 'keyword'}, 'text_passage_provenance': {'type': 'object', 'enabled': False}})
    assert migrate.call_args.kwargs['preparation_pool'] is None
    assert migrate.call_args.kwargs['limit'] == 1
    monkeypatch.setattr('sys.argv', ['reindex_embeddings', '--preparation-workers', '5'])
    with pytest.raises(SystemExit):
        job.main()


@pytest.mark.parametrize('previous_policy',['eq-passages-480-v4',passages.VERSION])
def test_source_upgrade_reuses_only_identical_passage_requests_and_advances_checkpoint(tmp_path,previous_policy):
    hit,client,embedder=fixture();source=hit['_source'];source.update(mime_type='text/html',text_extraction_version='old')
    path=tmp_path/hit['_id'];path.parent.mkdir(parents=True);path.write_text('<title>Guild</title><p>Preserved complete guild instructions.</p>')
    groups,patch,_=job.prepare(source,tmp_path);source.update(text_full=patch['text_full'],embedding_model=gemma.MODEL,text_chunking_version=previous_policy)
    old=[{**plan.provenance(),'version':previous_policy} for _,plan in groups]
    source['text_passage_provenance']={'version':previous_policy,'sources':old,'decoding':{'version':'source-decoding-v1'}}
    source['text']=[{'text_chunk':p.text,'title':p.title,'context':p.context,'kind':p.kind,'source_start':p.start,'source_end':p.end} for _,plan in groups for p in plan.passages]
    stats={};job.migrate_record(client,'eq',hit,tmp_path,embedder,diagnostics=stats)
    update=client.update.call_args.kwargs['doc']
    assert stats=={'reused_embeddings':1} and update['text_chunking_version']==passages.VERSION
    assert 'text' not in update and 'llm_summary_vector' not in update
    assert update['text_extraction_version']==extraction_version(source['id'])
    assert update['text_passage_provenance']['decoding']['version']=='source-decoding-v2'
    embedder.get_chunks_and_embeddings.assert_not_called()
    # A changed title/context changes the embedding request even if body text
    # and the old provenance hashes are identical. It must get new vectors.
    source['text'][0]['context']='Stale or unrelated context'
    embedder.reset_mock();job.migrate_record(client,'eq',hit,tmp_path,embedder)
    embedder.get_chunks_and_embeddings.assert_called()
    assert client.update.call_args.kwargs['doc']['text'][0]['text_chunk'].strip()=='Preserved complete guild instructions.'
