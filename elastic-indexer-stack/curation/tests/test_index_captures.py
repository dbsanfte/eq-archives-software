import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from common import CrawlError, digest
from conftest import manifest_for
from index_captures import Services, index_batch, read_batch, text_document
from indexer.gemma import DOCUMENT_PREFIX, load_tokenizer, MODEL


def published(row):
    manifest=manifest_for(row)
    return {'manifest':manifest,'manifest_sha256':digest(manifest),'publication':{'commit':'b'*40}}


def test_manifest_indexing_creates_missing_ids_preserves_source_and_skips_existing(candidate):
    root,row=candidate
    batch=published(row)
    class FakeServices:
        def __init__(self): self.docs={}; self.inputs=[]; self.enrichments=[]
        def exists(self,identifier): return identifier in self.docs
        def enrich(self,capture,source):
            self.enrichments.append(source)
            return {'llm_summary':'An EverQuest guild history and guide.', 'llm_content_flavour':'Guild Achievements',
                    'llm_tags':['raid'],'llm_guessed_date':None,'llm_extracted_dates':[],
                    'llm_model_name':'gpt-6-luna','llm_enrichment_signature':'c'*64}
        def embedding(self,text,**kwargs): self.inputs.append(text); return [0.01]*768
        def create(self,identifier,document): self.docs[identifier]=document; return True
    services=FakeServices()
    assert index_batch(root,batch,services) == {'created':1,'existing':0,'captures':1}
    identifier=batch['manifest']['captures'][0]['archive_path']
    doc=services.docs[identifier]
    assert identifier == 'websites/guild.example/20000101000000/eq/news.html'
    assert 'EverQuest guild history.' in doc['text_full']
    assert '[Guide](guide.html)' in doc['text_full']
    assert doc['url'] == 'https://web.archive.org/web/20000101000000/http://guild.example/eq/news.html'
    assert doc['capture_date'] == '2000-01-01T00:00:00+00:00'
    assert doc['archive_source_sha256'] == row['captures'][0]['sha256']
    assert doc['archive_commit'] == 'b'*40
    assert doc['llm_model_name']=='gpt-6-luna' and doc['llm_tags']==['raid']
    assert len(doc['llm_summary_vector'])==768
    assert doc['embedding_model'] == MODEL
    assert all('Page URL:' not in text for text in services.enrichments)
    assert all(len(load_tokenizer().encode(DOCUMENT_PREFIX+text).ids)<=480 for text in services.inputs)
    original=json.dumps(doc,sort_keys=True)
    assert index_batch(root,batch,services)['existing'] == 1
    assert json.dumps(services.docs[identifier],sort_keys=True) == original
    assert len(services.enrichments)==1


def test_entire_manifest_is_verified_before_any_index_or_embedding_request(candidate):
    root,row=candidate
    batch=published(row)
    (root/row['captures'][0]['path']).write_bytes(b'changed')
    class Forbidden:
        def exists(self,*args): raise AssertionError('No writes/embeddings before whole manifest validation')
    with pytest.raises(CrawlError): index_batch(root,batch,Forbidden())


def test_duplicate_passage_inputs_share_inference_without_dropping_source_occurrences(candidate,monkeypatch):
    from indexer import passages
    root,row=candidate
    source='Repeated raid evidence.\n\n'*4
    body='Repeated raid evidence.\n\n'
    chunks=tuple(passages.Passage(i*len(body),(i+1)*len(body),body,'Raid notes',
                                 'Post by Other' if i==2 else 'Post by Healer','post') for i in range(4))
    plan=passages.Plan(passages.digest(source),None,source,'Raid notes','source_text',(),chunks)
    monkeypatch.setattr('index_captures.passages.prepare',lambda *a,**k:plan)
    class FakeServices:
        def __init__(self):self.inputs=[];self.docs=[]
        def exists(self,*a):return False
        def enrich(self,*a):return {'llm_summary':'A guild raid history.'}
        def embedding(self,text,**kwargs):self.inputs.append((text,kwargs));return [1.]+[0.]*767
        def create(self,key,doc):self.docs.append(doc);return True
    services=FakeServices()
    assert index_batch(root,published(row),services)['created']==1
    passage_calls=[x for x in services.inputs if x[1]]
    assert len(passage_calls)==2 and len(services.docs[0]['text'])==4
    saved=services.docs[0]['text']
    assert [(p['source_start'],p['source_end']) for p in saved]==[(p.start,p.end) for p in chunks]
    assert saved[0]['vector']==saved[1]['vector'] and saved[0]['vector'] is not saved[1]['vector']


def test_new_capture_uses_same_japanese_decoder_for_reader_enrichment_and_embeddings(candidate):
    root,row=candidate;capture=row['captures'][0]
    phrase='こんにちは。ギルドのみんなで①番の髙い山へ行きましょう。'
    raw=('<html><meta charset="utf-8"><title>冒険記録</title><p>'+phrase+'</p></html>').encode('cp932')
    (root/capture['path']).write_bytes(raw);capture.update(content_type='text/html',bytes=len(raw),sha256=digest(raw))
    class Services:
        def exists(self,*a):return False
        def enrich(self,capture,text):
            assert phrase in text
            return {'llm_summary':'An EverQuest guild adventure.'}
        def embedding(self,text,**k):return [1.]+[0.]*767
        def create(self,key,document):
            assert phrase in document['text_full']
            assert any(phrase in c['text_chunk'] for c in document['text'])
            assert document['text_passage_provenance']['decoding']['encoding']=='cp932'
            assert document['archive_source_sha256']==digest(raw)
            return True
    assert index_batch(root,published(row),Services())=={'created':1,'existing':0,'captures':1}
    assert (root/capture['path']).read_bytes()==raw


def test_archived_error_remains_captured_without_paying_for_enrichment_or_embedding(candidate):
    root,row=candidate
    capture=row['captures'][0]
    raw=b'<title>System Message: Error</title><h1>Error</h1><p>The post you selected no longer exists.</p>'
    (root/capture['path']).write_bytes(raw)
    capture.update(content_type='text/html',bytes=len(raw),sha256=digest(raw))
    class SourcesOnly:
        def exists(self,*args):return False
        def enrich(self,*args):raise AssertionError('No paid enrichment of an archived error')
        def embedding(self,*args):raise AssertionError('No error embedding')
        def create(self,*args):raise AssertionError('Do not publish a semantic document')
    assert index_batch(root,published(row),SourcesOnly())=={'created':0,'existing':0,'captures':1,'preserved_files':1}
    assert (root/capture['path']).read_bytes()==raw


def test_enrichment_failure_leaves_new_document_pending_and_never_writes_fallback(candidate):
    root,row=candidate
    class Pending:
        def exists(self,*args):return False
        def enrich(self,*args):raise CrawlError('Enrichment dollar budget reached')
        def embedding(self,*args):raise AssertionError('No embedding before required enrichment')
        def create(self,*args):raise AssertionError('No unenriched document should be created')
    with pytest.raises(CrawlError,match='Enrichment dollar budget'):
        index_batch(root,published(row),Pending())


def test_read_batch_rejects_missing_publication_stale_hash_and_path_escape(candidate):
    root,row=candidate
    path=root/'approved.json'
    batch=published(row)
    path.write_text(json.dumps(batch))
    assert read_batch(root,'approved.json',batch['manifest_sha256']) == batch
    with pytest.raises(CrawlError): read_batch(root,'approved.json','stale')
    batch['publication']['commit']='not-published'
    path.write_text(json.dumps(batch))
    with pytest.raises(CrawlError): read_batch(root,'approved.json',batch['manifest_sha256'])
    outside=root.parent/'outside.json'
    outside.write_text('{}')
    with pytest.raises(CrawlError): read_batch(root,'../outside.json','stale')


def service(tmp_path,handler):
    for name in ('es_username','es_password','openai_api_key'):
        (tmp_path/name).write_text('dummy-'+name)
    instance=Services(tmp_path)
    instance.client.close(); instance.embedder.close()
    instance.client=httpx.Client(transport=httpx.MockTransport(handler))
    instance.embedder=httpx.Client(transport=httpx.MockTransport(handler))
    return instance


def test_es_requests_are_exact_create_only_and_conflicts_preserve_other_writer(tmp_path):
    seen=[]
    def handler(request):
        seen.append(request)
        if request.method=='HEAD': return httpx.Response(404)
        return httpx.Response(409)
    instance=service(tmp_path,handler)
    try:
        identifier='websites/guild.example/20000101000000/news.php?q=old&x=1'
        assert instance.exists(identifier) is False
        assert instance.create(identifier,{'id':identifier}) is False
        assert seen[1].method=='PUT' and '/_create/' in str(seen[1].url)
        assert b'%3Fq%3Dold%26x%3D1' in seen[1].url.raw_path
    finally: instance.close()


def test_create_sends_compact_float32_vectors_without_rounding_other_metadata(tmp_path):
    from array import array
    values=array('f',[.123456789,-.987654321,-0.])
    document={'id':'sample','text':[{'vector':list(values),'text_chunk':'EverQuest guild'}],
              'score':.12345678901234567}
    def handler(request):
        assert request.headers['content-type']=='application/json'
        saved=json.loads(request.content)
        assert array('f',saved['text'][0]['vector']).tobytes()==values.tobytes()
        assert saved['score']==document['score'] and saved['text'][0]['text_chunk']=='EverQuest guild'
        return httpx.Response(201,json={'result':'created'})
    instance=service(tmp_path,handler)
    try:assert instance.create('sample',document)
    finally:instance.close()


@pytest.mark.parametrize('vector',[[0.1]*767,[float('inf')]*768,[0.0]*768,['bad']*768,None,42,{}])
def test_bad_vectors_fail_without_indexing(tmp_path,vector):
    instance=service(tmp_path,lambda request:httpx.Response(200,content=json.dumps({'data':[{'embedding':vector}]})))
    try:
        with pytest.raises(CrawlError,match='vector'): instance.embedding('EQ source')
    finally: instance.close()


def test_embeddings_are_serial_prefixed_and_retry_transient_busy_response(tmp_path):
    requests=[]
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(503) if len(requests)==1 else httpx.Response(200,json={'model':MODEL,'data':[{'embedding':[0.1]*768}]})
    instance=service(tmp_path,handler)
    try:
        with patch('index_captures.time.sleep'):
            assert len(instance.embedding('Complete source.'))==768
        assert len(requests)==2
        assert all(row['input']=='title: none | text: Complete source.' for row in requests)
        assert all(row['model']==MODEL for row in requests)
    finally: instance.close()


def test_default_embedding_requests_use_cuda_and_leave_vulkan_for_live_queries(tmp_path, monkeypatch):
    monkeypatch.delenv('EMBEDDING_URL', raising=False)
    monkeypatch.setattr('index_captures.time.sleep', lambda _: None)
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'model':MODEL,'data':[{'embedding':[1.0]+[0.0]*767}]})
    services=service(tmp_path,handler)
    try:
        assert len(services.embedding('EverQuest cleric guild'))==768
        assert len(seen)==1
        assert seen[0].url.host=='nomic-embeddings-rtx3080.eqarchives-es.svc.cluster.local'
        assert seen[0].url.path=='/v1/embeddings'
    finally:
        services.close()


def test_successful_model_requests_have_no_intentional_throttle(tmp_path):
    instance=service(tmp_path,lambda request:httpx.Response(200,json={
        'model':MODEL,'data':[{'embedding':[1.]+[0.]*767}]}))
    try:
        with patch('index_captures.time.sleep') as sleep:
            instance.embedding('A complete guide.')
        sleep.assert_not_called()
    finally:instance.close()


def test_model_mismatch_cannot_index_vectors_from_old_backend(tmp_path):
    instance=service(tmp_path, lambda request: httpx.Response(200, json={
        'model':'old-nomic', 'data':[{'embedding':[1.0]+[0.0]*767}]}))
    try:
        with pytest.raises(CrawlError, match='different model'):
            instance.embedding('Cleric')
    finally:
        instance.close()
