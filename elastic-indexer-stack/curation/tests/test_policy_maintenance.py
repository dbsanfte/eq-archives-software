from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from indexer import gemma,passages
from indexer.embedding_cache import EmbeddingCache
from indexer.html_extraction import WEBSITE_EXTRACTION_VERSION
from policy_sources import Sources,SourceError
from policy_maintenance import (MaintenanceServices,ServiceError,State,YieldWork,foreground,
                                identity,newer,repair,run)
from state import connect

V=[1.0]+[0.0]*767
IMAGE='dbsanfte/frontend@sha256:'+'1'*64


def fixture(tmp_path,raw='<title>Guild news</title><p>Our guild defeated Nagafen.</p>',*,changed=False,identifier=None):
    identifier=identifier or 'websites/guild.example/20000101000000/index.html'
    reader='Complete archival reader with all original text.'
    plan=passages.prepare(reader,identifier,html_source=raw,mime_type='text/html')
    source=dict(id=identifier,file_type='text',mime_type='text/html',title='Guild news',text_full=reader,
        embedding_model=gemma.MODEL,text_extraction_version=WEBSITE_EXTRACTION_VERSION,
        text_chunking_version='eq-passages-480-v10',capture_date='2000-01-01',last_indexed='2000-01-02',
        llm_summary='Original paid summary',llm_tags=['Guild'],discussion={'id':'preserved-discussion'},
        archive_source_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        text_passage_provenance={'version':'eq-passages-480-v10','sources':[{**plan.provenance(),'version':'eq-passages-480-v10'}]},
        text=[dict(text_chunk=p.text,title=p.title,context=p.context,kind=p.kind,
                   source_start=p.start,source_end=p.end,vector=V) for p in plan.passages])
    if changed:source['text'][0]['context']='Old incorrect context'
    root=tmp_path/'archive';file=root/identifier;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(raw)
    hit={'_id':identifier,'_index':'eq-archive-0.4','_seq_no':17,'_primary_term':2,'_source':source}
    return hit,Sources([root],tmp_path/'publisher.git')


class Services:
    def __init__(self,hits):self.hits={h['_id']:deepcopy(h) for h in hits};self.updates=[];self.inputs=[];self.failures={}
    def get(self,identifier):
        if identifier in self.failures:raise self.failures[identifier]
        return deepcopy(self.hits[identifier])
    def update(self,hit,patch):
        assert self.hits[hit['_id']]['_seq_no']==hit['_seq_no']
        self.updates.append(deepcopy(patch));self.hits[hit['_id']]['_source'].update(deepcopy(patch))
        self.hits[hit['_id']]['_seq_no']+=1
    def vectors(self,inputs,check):
        check();self.inputs+=inputs
        assert all(len(gemma.load_tokenizer().encode(value).ids)<=480 for value in inputs)
        return [V.copy() for _ in inputs]
    def select(self,cursor):
        return [k for k,h in sorted(self.hits.items()) if k>cursor and h['_source']['text_chunking_version']!=passages.VERSION][:100]


def test_policy_upgrade_reuses_exact_bound_vectors_and_preserves_reader_enrichment_and_dates(tmp_path):
    hit,sources=fixture(tmp_path);services=Services([hit]);original=deepcopy(hit)
    try:result=repair(services,sources,hit['_id'],lambda:None)
    finally:sources.close()
    assert result=='reused' and services.inputs==[]
    patch=services.updates[0]
    assert set(patch)=={'text_chunking_version','text_passage_provenance'}
    assert patch['text_chunking_version']==passages.VERSION
    assert hit==original
    for field in ('llm_summary','llm_tags','last_indexed','capture_date','discussion','text_full'):
        assert services.hits[hit['_id']]['_source'][field]==original['_source'][field]


def test_changed_context_is_reembedded_and_all_writes_stay_atomic(tmp_path):
    hit,sources=fixture(tmp_path,changed=True);services=Services([hit])
    try:assert repair(services,sources,hit['_id'],lambda:None)=='updated'
    finally:sources.close()
    assert services.inputs and len(services.updates)==1
    patch=services.updates[0]
    assert patch['text'][0]['vector']==V and patch['text_chunking_version']==passages.VERSION
    assert 'text_full' not in patch and 'llm_summary_vector' not in patch


def test_generated_status_clears_every_semantic_field_without_inference(tmp_path):
    hit,sources=fixture(tmp_path,raw='<h3>Error. Page cannot be displayed. Please contact your service provider for more details. (7)</h3>')
    # A prior policy stored a noisy paragraph and both enrichment vectors.
    hit['_source']['text']=[{'text_chunk':'Outdated error passage','vector':V}]
    services=Services([hit])
    try:assert repair(services,sources,hit['_id'],lambda:None)=='updated'
    finally:sources.close()
    patch=services.updates[0]
    assert patch['text']==[] and patch['llm_summary_vector'] is None and patch['llm_image_text_vector'] is None
    assert services.inputs==[] and 'llm_summary' not in patch and 'text_full' not in patch


@pytest.mark.parametrize('change,result',[({'embedding_model':'another-model'},'different_model'),
    ({'file_type':'image'},'different_model'),({'text_chunking_version':'eq-passages-480-v999'},'newer_policy'),
    ({'text_chunking_version':'unknown-future-policy'},'newer_policy')])
def test_background_worker_never_downgrades_newer_policies_or_other_models(tmp_path,change,result):
    hit,sources=fixture(tmp_path);hit['_source'].update(change);services=Services([hit])
    assert repair(services,sources,hit['_id'],lambda:None)==result
    assert not services.updates and not services.inputs


def test_external_repair_resolves_only_a_verified_checkpoint(tmp_path):
    hit,sources=fixture(tmp_path);services=Services([hit])
    repair(services,sources,hit['_id'],lambda:None)
    assert repair(services,sources,hit['_id'],lambda:None)=='external_current'
    services.hits[hit['_id']]['_source']['text'][0]['context']='Changed request under an incorrect completion marker'
    with pytest.raises(SourceError,match='checkpoint_integrity'):repair(services,sources,hit['_id'],lambda:None)
    services.hits[hit['_id']]['_source']['text_full']='Changed outside the bound checkpoint'
    with pytest.raises(SourceError,match='reader_integrity'):repair(services,sources,hit['_id'],lambda:None)
    assert len(services.updates)==1


def test_reader_repairs_require_the_original_bytes_and_preserve_all_metadata(tmp_path):
    hit,sources=fixture(tmp_path);hit['_source']['text_extraction_version']='old-extraction'
    services=Services([hit]);repair(services,sources,hit['_id'],lambda:None)
    patch=services.updates[0]
    assert 'Nagafen' in patch['text_full'] and 'Page URL' in patch['text_full']
    assert patch['text_extraction_version']==WEBSITE_EXTRACTION_VERSION
    assert patch['text_passage_provenance']['sources'][0]['source_sha256']==passages.digest(patch['text_full'])
    assert not set(patch)&{'capture_date','last_indexed','llm_summary','llm_tags','discussion'}


def test_mail_readers_and_binary_extraction_checkpoints_stay_unchanged(tmp_path):
    for identifier,mime in [('mailing-lists/guild/42.txt','text/plain'),('websites/guild.example/20000101000000/guide.pdf','application/pdf')]:
        hit,_=fixture(tmp_path,identifier=identifier)
        source=hit['_source'];source.update(mime_type=mime,text_extraction_version='retained-binary-or-mail')
        p=passages.prepare(source['text_full'],identifier,source['title'],mime_type=mime)
        source['text_passage_provenance']={'version':'eq-passages-480-v10','sources':[{**p.provenance(),'version':'eq-passages-480-v10'}]}
        source['text']=[];services=Services([hit])
        class NoSource:
            def read(self,*_):raise AssertionError('Only textual websites require raw HTML')
        repair(services,NoSource(),identifier,lambda:None)
        assert 'text_extraction_version' not in services.updates[0] and 'text_full' not in services.updates[0]


def test_one_bad_source_advances_to_later_records_with_private_durable_diagnostics(tmp_path,monkeypatch):
    a,sources=fixture(tmp_path,identifier='websites/guild.example/20000101000000/a.html',changed=True)
    b,_=fixture(tmp_path,identifier='websites/guild.example/20000101000000/b.html',changed=True)
    (sources.roots[0]/a['_id']).write_text('Changed bytes that must not replace the capture')
    services=Services([a,b]);state=State(tmp_path/'state');monkeypatch.setenv('IMPORT_IMAGE',IMAGE)
    job='eqarchives-policy-'+identity()+'-1'
    result=run(tmp_path,state,services,sources,job,should_yield=lambda:False)
    assert result['terminal'] and result['scanned']==2 and result['failed']==1 and result['updated']==1 and result['unresolved']==1
    assert services.hits[a['_id']]['_source']['text_chunking_version']=='eq-passages-480-v10'
    row=state.db.connection().execute('SELECT * FROM policy_failures').fetchone()
    assert row['reason']=='source_integrity' and row['source_sha256']==hashlib.sha256(b'Changed bytes that must not replace the capture').hexdigest()
    assert 'Changed bytes' not in json.dumps(result)
    state.close();resumed=State(tmp_path/'state')
    assert resumed.snapshot()['updated']==1 and resumed.snapshot()['unresolved']==1
    assert resumed.deferred(a['_id']) and resumed.due()==[]
    resumed.close();sources.close()


def test_retries_resolve_after_an_acknowledged_repair_and_keep_audit_history(tmp_path,monkeypatch):
    hit,sources=fixture(tmp_path,changed=True);services=Services([hit]);state=State(tmp_path/'state')
    state.reject(hit['_id'],'source_unavailable',hit['_source']['archive_source_sha256'],'old-job')
    state.db.connection().execute('UPDATE policy_failures SET retry_at=0');state.db.connection().commit()
    services.failures[hit['_id']]=SourceError('concurrent_update')
    result=run(tmp_path,state,services,sources,'eqarchives-policy-'+identity()+'-1',maximum=1,should_yield=lambda:False)
    assert result['unresolved']==1 and result['failed']==1 and result['cursor']==''
    del services.failures[hit['_id']]
    state.db.connection().execute('UPDATE policy_failures SET retry_at=0');state.db.connection().commit()
    result=run(tmp_path,state,services,sources,'eqarchives-policy-'+identity()+'-2',maximum=1,should_yield=lambda:False)
    assert result['unresolved']==0 and result['updated']==1 and result['cursor']==''
    assert state.db.connection().execute("SELECT COUNT(*) FROM policy_events WHERE action='resolved'").fetchone()[0]==1
    state.close();sources.close()


def test_budget_and_foreground_yields_never_advance_an_incomplete_record(tmp_path):
    hit,sources=fixture(tmp_path,changed=True);services=Services([hit]);state=State(tmp_path/'state')
    result=run(tmp_path,state,services,sources,'eqarchives-policy-'+identity()+'-1',should_yield=lambda:True)
    assert result['phase']=='yielded' and result['cursor']=='' and result['scanned']==0 and not services.updates
    services.failures[hit['_id']]=ServiceError('authentication')
    result=run(tmp_path,state,services,sources,'eqarchives-policy-'+identity()+'-2',should_yield=lambda:False)
    assert result['phase']=='retry_wait' and result['cursor']=='' and result['failed']==0
    for name,maximum,seconds in [('bad-job',1,1),('eqarchives-policy-'+identity()+'-3',0,1),('eqarchives-policy-'+identity()+'-3',1,601)]:
        with pytest.raises(ValueError):run(tmp_path,state,services,sources,name,maximum=maximum,seconds=seconds)
    assert newer('other-policy') and not newer('eq-passages-480-v1')
    state.close();sources.close()


def test_time_bound_yields_without_work_and_idle_sweeps_do_not_query_again(tmp_path):
    hit,sources=fixture(tmp_path);services=Services([hit]);state=State(tmp_path/'state')
    moments=iter([0,2])
    result=run(tmp_path,state,services,sources,'eqarchives-policy-'+identity()+'-1',
               seconds=1,clock=lambda:next(moments),should_yield=lambda:False)
    assert result['phase']=='yielded' and result['scanned']==0
    state.exhausted()
    class NoWork:
        def select(self,*_):raise AssertionError('An exhausted sweep must wait instead of spin')
    result=run(tmp_path,state,NoWork(),sources,'eqarchives-policy-'+identity()+'-2',should_yield=lambda:False)
    assert result['phase']=='sweep_waiting' and result['scanned']==0
    state.close();sources.close()


def test_repeated_hard_worker_interruptions_quarantine_only_that_record_and_later_records_continue(tmp_path):
    first,sources=fixture(tmp_path,identifier='websites/guild.example/20000101000000/a.html',changed=True)
    later,_=fixture(tmp_path,identifier='websites/guild.example/20000101000000/b.html',changed=True)
    state=State(tmp_path/'state')
    for attempt in range(1,4):
        assert state.begin(first['_id'],f'eqarchives-policy-{identity()}-{attempt}')
        state.close();state=State(tmp_path/'state')
    services=Services([first,later])
    result=run(tmp_path,state,services,sources,f'eqarchives-policy-{identity()}-4',should_yield=lambda:False)
    assert result['unresolved']==1 and result['updated']==1 and result['phase']=='sweep_waiting'
    assert state.inflight() is None
    row=state.db.connection().execute('SELECT reason FROM policy_failures').fetchone()
    assert row[0]=='repeated_worker_interruption'
    assert services.hits[first['_id']]['_source']['text_chunking_version']=='eq-passages-480-v10'
    state.close();sources.close()


def test_known_transport_outages_do_not_consume_interruption_retries(tmp_path):
    hit,sources=fixture(tmp_path,changed=True);services=Services([hit]);state=State(tmp_path/'state')
    services.failures[hit['_id']]=ServiceError('embedding_unavailable')
    for attempt in range(1,5):
        result=run(tmp_path,state,services,sources,f'eqarchives-policy-{identity()}-{attempt}',should_yield=lambda:False)
        assert result['phase']=='retry_wait' and state.inflight() is None and result['unresolved']==0
    state.close();sources.close()


def test_only_one_background_process_can_own_the_shared_cursor_even_if_a_job_disappears(tmp_path):
    state=State(tmp_path/'state')
    with pytest.raises(ServiceError,match='maintenance_already_running'):State(tmp_path/'state')
    state.close()
    resumed=State(tmp_path/'state');resumed.close()


def test_index_changes_have_separate_cursors_and_cannot_resolve_another_indexs_failures(tmp_path,monkeypatch):
    monkeypatch.setenv('POLICY_INDEX','eq-archive-0.4')
    state=State(tmp_path/'state');first_key=identity()
    state.reject('websites/guild.example/20000101000000/a.html','source_unavailable',None,'original')
    state.advance('websites/guild.example/20000101000000/b.html','updated')
    state.close()
    monkeypatch.setenv('POLICY_INDEX','eq-archive-0.5')
    fresh=State(tmp_path/'state')
    assert identity()!=first_key and fresh.snapshot()['cursor']=='' and fresh.snapshot()['unresolved']==0
    assert fresh.due()==[]
    fresh.advance('websites/guild.example/20000101000000/a.html','updated')
    assert fresh.db.connection().execute('SELECT resolved_at FROM policy_failures').fetchone()[0] is None
    fresh.close()
    monkeypatch.setenv('POLICY_INDEX','eq-archive-0.4')
    original=State(tmp_path/'state')
    assert original.snapshot()['unresolved']==1 and original.snapshot()['cursor'].endswith('/b.html')
    original.close()


def test_foreground_detection_ignores_budget_waits_and_human_attention_but_obeys_new_policy(tmp_path):
    from policy_scheduler import write_control
    with connect(tmp_path) as store:
        store.db.execute("INSERT INTO batches VALUES ('site','index_budget_waiting','{}','',NULL,NULL,NULL,'now','now')");store.db.commit()
        write_control(tmp_path,True)
        assert foreground(tmp_path) is False
        store.db.execute("UPDATE batches SET state='indexing'");store.db.commit();write_control(tmp_path,True);assert foreground(tmp_path)
        store.db.execute("UPDATE batches SET state='index_failed'");store.db.commit();write_control(tmp_path,True);assert not foreground(tmp_path)
        control=tmp_path/'indexing-state/policy-control.json'
        data=json.loads(control.read_text());data['key']='future';control.write_text(json.dumps(data));assert foreground(tmp_path)
        write_control(tmp_path,False);assert foreground(tmp_path)
    with pytest.raises(ServiceError):foreground(tmp_path/'absent')


def test_partial_embedding_batches_are_cached_before_yield_and_complete_identity_is_validated(tmp_path):
    calls=[];service=MaintenanceServices.__new__(MaintenanceServices);service.model=gemma.MODEL;service.embedding_url='http://cuda'
    service.embedding_cache=EmbeddingCache(tmp_path/'cache.sqlite','fixture')
    def response(request):
        body=json.loads(request.content);calls.append(body['input'])
        return httpx.Response(200,json={'model':gemma.MODEL,'data':[{'index':i,'embedding':V} for i in reversed(range(len(body['input'])))]})
    service.embedder=httpx.Client(transport=httpx.MockTransport(response))
    count=0
    def stop():
        nonlocal count
        count+=1
        if count==3:raise YieldWork()
    inputs=[f'title: none | text: Independent input {i}' for i in range(12)]
    with pytest.raises(YieldWork):service.vectors(inputs,stop)
    assert len(calls)==1 and len(calls[0])==8
    vectors=service.vectors(inputs,lambda:None)
    assert vectors==[V]*12 and len(calls)==2 and len(calls[1])==4
    service.embedding_cache.close();service.embedder.close()


@pytest.mark.parametrize('kind',['model','index','missing','nan','zero','http','transport'])
def test_malformed_or_incomplete_embedding_responses_fail_before_any_write(kind):
    service=MaintenanceServices.__new__(MaintenanceServices);service.model=gemma.MODEL;service.embedding_url='http://cuda';service.embedding_cache=None
    def response(request):
        if kind=='transport':raise httpx.ConnectError('private upstream details')
        if kind=='http':return httpx.Response(503)
        payload={'model':gemma.MODEL,'data':[{'index':0,'embedding':V}]}
        if kind=='model':payload['model']='wrong'
        elif kind=='index':payload['data'][0]['index']=True
        elif kind=='missing':payload['data']=[]
        elif kind=='zero':payload['data'][0]['embedding']=[0.0]*768
        elif kind=='nan':return httpx.Response(200,content=b'{"model":"'+gemma.MODEL.encode()+b'","data":[{"index":0,"embedding":[NaN]}]}')
        return httpx.Response(200,json=payload)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        service.embedder=client
        with pytest.raises(ServiceError) as caught:service.vectors(['title: none | text: test'],lambda:None)
        assert 'private' not in str(caught.value)


def test_search_get_and_optimistic_updates_keep_the_index_and_response_identity_bound(tmp_path):
    hit,_=fixture(tmp_path);seen=[]
    def response(request):
        seen.append(request)
        if request.url.path.endswith('/_search'):
            body=json.loads(request.content)
            assert body['_source'] is False and body['size']==100
            assert {'term':{'embedding_model':gemma.MODEL}} in body['query']['bool']['filter']
            return httpx.Response(200,json={'hits':{'hits':[{'_id':hit['_id'],'_index':hit['_index']}]}})
        if '/_update/' in request.url.path:
            assert request.url.params['if_seq_no']=='17' and request.url.params['if_primary_term']=='2'
            assert set(json.loads(request.content))=={'doc'}
            return httpx.Response(200,json={'_id':hit['_id'],'_index':hit['_index'],'result':'updated'})
        assert 'text.vector' not in request.url.params['_source_includes']
        return httpx.Response(200,json=hit)
    service=MaintenanceServices.__new__(MaintenanceServices);service.index=hit['_index'];service.es_url='https://archive'
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        service.client=client
        assert service.select('')==[hit['_id']]
        assert service.get(hit['_id'])==hit
        service.update(hit,{'text_chunking_version':passages.VERSION})
    assert all('eq-archive-0.4' in request.url.path for request in seen)


@pytest.mark.parametrize('status,expected',[(409,SourceError),(404,SourceError),(401,ServiceError),(403,ServiceError),(503,ServiceError)])
def test_elasticsearch_error_categories_never_include_upstream_responses(status,expected):
    service=MaintenanceServices.__new__(MaintenanceServices);service.es_url='https://archive'
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(status,text='private response'))) as client:
        service.client=client
        with pytest.raises(expected) as caught:service.request('GET','/index/_doc/known')
        assert 'private' not in str(caught.value)


def test_response_byte_bounds_and_transport_failures_are_explicit():
    service=MaintenanceServices.__new__(MaintenanceServices);service.es_url='https://archive'
    for transport,exception in [(httpx.MockTransport(lambda _:httpx.Response(200,text='x'*128)),SourceError),
                                (httpx.MockTransport(lambda _:httpx.Response(200,text='invalid json')),ServiceError)]:
        with httpx.Client(transport=transport) as client:
            service.client=client
            with pytest.raises(exception):service.request('GET','/index',limit=32)
    def failure(_):raise httpx.ConnectError('private transport failure')
    with httpx.Client(transport=httpx.MockTransport(failure)) as client:
        service.client=client
        with pytest.raises(ServiceError):service.request('GET','/index')


@pytest.mark.parametrize('change',[{'_id':'wrong'},{'_index':'wrong'},{'_seq_no':True},{'_seq_no':-1},{'_primary_term':0},{'_source':{'id':'wrong'}}])
def test_inconsistent_get_identity_cannot_reach_the_write_path(tmp_path,change):
    hit,_=fixture(tmp_path);bad={**hit,**change}
    service=MaintenanceServices.__new__(MaintenanceServices);service.es_url='https://archive';service.index=hit['_index']
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=bad))) as client:
        service.client=client
        with pytest.raises(ServiceError):service.get(hit['_id'])


def test_invalid_search_order_and_unacknowledged_updates_cannot_advance_checkpoints(tmp_path):
    hit,_=fixture(tmp_path);service=MaintenanceServices.__new__(MaintenanceServices);service.es_url='https://archive';service.index=hit['_index']
    for data in [{'hits':{'hits':[{'_id':'a','_index':hit['_index']}]}}, {'hits':{'hits':[]}}, {'hits':None}]:
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=data))) as client:
            service.client=client
            if data=={'hits':{'hits':[]}}:assert service.select('b')==[]
            else:
                with pytest.raises(ServiceError):service.select('b')
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'result':'created'}))) as client:
        service.client=client
        with pytest.raises(ServiceError):service.update(hit,{'text_chunking_version':passages.VERSION})


@pytest.mark.parametrize('phase,code',[('bounded',0),('retry_wait',1)])
def test_entrypoint_closes_local_resources_and_reports_its_bounded_outcome(tmp_path,monkeypatch,capsys,phase,code):
    import policy_maintenance as module
    closed=[]
    class Resource:
        def close(self):closed.append(self)
    monkeypatch.setattr(module,'State',lambda _:Resource())
    monkeypatch.setattr(module,'MaintenanceServices',lambda:Resource())
    monkeypatch.setattr(module,'Sources',lambda *_:Resource())
    monkeypatch.setattr(module,'run',lambda root,state,services,sources,job:{'phase':phase})
    monkeypatch.setattr('sys.argv',['policy_maintenance.py','--root',str(tmp_path)])
    monkeypatch.setenv('POLICY_JOB_NAME','eqarchives-policy-'+identity()+'-1')
    assert module.main()==code and len(closed)==3
    assert json.loads(capsys.readouterr().out)=={'phase':phase}


def test_source_proven_title_repairs_bind_vectors_to_the_saved_title(tmp_path):
    title='黒い森のギルド日記'
    raw=('<html><title>'+title+'</title><body><p>'+('仲間たちと冒険へ出かけました。'*20)+'</p></body></html>').encode('cp932')
    hit,sources=fixture(tmp_path);source=hit['_source']
    source['title']=title.encode('cp932').decode('latin1')
    source['archive_source_sha256']=hashlib.sha256(raw).hexdigest()
    (tmp_path/'archive'/hit['_id']).write_bytes(raw)
    services=Services([hit]);repair(services,sources,hit['_id'],lambda:None)
    patch=services.updates[0]
    assert patch['title']==title and services.inputs
    assert all(p['title']==title for p in patch['text'])
    assert repair(services,sources,hit['_id'],lambda:None)=='external_current'


def test_initial_gemma_readers_without_provenance_are_rebuilt_from_verified_website_sources(tmp_path):
    hit,sources=fixture(tmp_path);source=hit['_source']
    source['text_chunking_version']='embeddinggemma2-paragraphs-480-overlap48-v1'
    source.pop('text_passage_provenance');source['text_extraction_version']=WEBSITE_EXTRACTION_VERSION
    services=Services([hit]);repair(services,sources,hit['_id'],lambda:None)
    patch=services.updates[0]
    assert 'Nagafen' in patch['text_full'] and 'Page URL' in patch['text_full']
    assert patch['text_chunking_version']==passages.VERSION and services.inputs
    assert repair(services,sources,hit['_id'],lambda:None)=='external_current'
    assert not set(patch)&{'llm_summary','llm_tags','capture_date','last_indexed'}
