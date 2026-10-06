import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from common import CrawlError, digest
from conftest import manifest_for
from index_captures import Services, index_batch, read_batch, text_document
from indexer.chunking import DOCUMENT_PREFIX, load_tokenizer


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
        def embedding(self,text): self.inputs.append(text); return [0.01]*768
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
        return httpx.Response(503) if len(requests)==1 else httpx.Response(200,json={'data':[{'embedding':[0.1]*768}]})
    instance=service(tmp_path,handler)
    try:
        with patch('index_captures.time.sleep'):
            assert len(instance.embedding('Complete source.'))==768
        assert len(requests)==2
        assert all(row['input']=='search_document: Complete source.' for row in requests)
        assert all(row['model']=='text-embedding-nomic-embed-text-v1.5@q8_0' for row in requests)
    finally: instance.close()
