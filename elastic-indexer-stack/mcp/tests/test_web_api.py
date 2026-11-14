import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from eqarchives_mcp.server import create_app
from eqarchives_mcp.web_api import Builder, DATES, FACETS, HIGHLIGHT, LIST_FIELDS, MAX_BODY, TEXT
from test_archive import make_archive, VECTOR

URL='/elasticsearch/eq-archive/_search'


def test_browser_search_rebuilds_bounds_projection_highlights_and_hybrid_filters():
    seen=[]
    def es(request):
        seen.append(json.loads(request.content))
        assert request.url.path == '/eq-archive/_search'
        assert dict(request.url.params) == {'preference':'eqarchives-mcp-research'}
        return httpx.Response(200,json={'hits':{'hits':[],'total':{'value':0,'relation':'eq'}}})
    body={'size':50,'from':0,'_source':{'includes':['*']},'highlight':{'encoder':'default','pre_tags':['<img onerror=x>']},
          'query':{'bool':{'should':[{'multi_match':{'query':'ancient cyclops','fields':['text_full^1','llm_summary^1','llm_image_text^1'],'type':'phrase_prefix','operator':'and'}}],'minimum_should_match':1}},
          'sort':[{'capture_date':{'order':'desc','missing':'_last'}},{'id':{'order':'asc','missing':'_last'}},{'_doc':'asc'}],
          'knn':[{'field':field,'k':50,'num_candidates':250,'boost':1000,'query_vector':VECTOR,'filter':{'term':{'domain_name':'example.org'}}} for field in ('image_vector','llm_summary_vector','llm_image_text_vector','text.vector')]}
    with TestClient(create_app(make_archive(es))) as client:
        assert client.post(URL,json=body).status_code==200
    result=seen[0]
    assert result['timeout']=='8s' and result['track_total_hits']==1001
    assert result['_source']==LIST_FIELDS and 'text_full' not in result['_source']
    assert result['highlight']==HIGHLIGHT
    assert result['query']['bool']['should'][0]['multi_match']['max_expansions']==20
    assert all(branch['boost']==10 and branch['filter'][-1]=={'term':{'embedding_model':'embeddinggemma-2-768-v1'}} for branch in result['knn'])


@pytest.mark.parametrize('extra',[
 {'script_fields':{'pwn':{'script':'return 1;'}}}, {'runtime_mappings':{}}, {'profile':True},
 {'explain':True},{'stored_fields':['*']},{'search_after':[0]}, {'size':10000},{'from':1000},
 {'size':101},{'size':True},{'query':{'script':{'script':'return true'}}},
 {'query':{'bool':{'must':[{'function_score':{}}]}}}, {'query':{'wildcard':{'text_full':'*'}}},
 {'query':{'ids':{'values':['a','b']}}}, {'size':20,'query':{'ids':{'values':['a']}}},
 {'aggs':{'evil':{'terms':{'field':'text_full'}}}}, {'query':{'term':{'unknown':'x'}}},
 {'query':{'range':{'capture_date':{'gte':'now-9999999y'}}}},
 {'query':{'nested':{'path':'text','query':{'match_all':{}}}}},
 {'query':{'multi_match':{'query':'a','fields':['*'],'type':'best_fields'}}},
 {'query':{'regexp':{'id':'.*'}}}, {'query':{'regexp':{'id':'websites/a/abc'}}},
 {'query':{'regexp':{'id':'websites/example\\.org/[0-9]{14}/(a+)+'}}},
 {'query':{'bool':{'minimum_should_match':10000}}},
 {'query':{'prefix':{'id':'a'}}}, {'query':{'match_phrase':{'text_full':'x'}}},
 {'query':{'exists':{'field':'arbitrary'}}}, {'query':{'match_all':{'boost':100}}},
 {'query':{'bool':{'boost':10}}}, {'query':{'terms':{'domain_name':['x']*21}}},
 {'query':{'term':{'domain_name':{'value':'x','boost':100}}}},
 {'sort':[{'text_full':'desc'}]}, {'sort':[{'id':{'order':'asc','missing':'_first'}}]},
 {'query':{'multi_match':{'query':'a','fields':['text_full','llm_summary','llm_image_text'],'type':'evil'}}},
 {'query':{'query_string':{'query':'a'*1001,'fields':['text_full','llm_summary','llm_image_text']}}},
 {'query':{'query_string':{'query':'a '*101,'fields':['text_full','llm_summary','llm_image_text']}}},
 {'query':{'bool':{'filter':[{'bool':{'filter':[]}}]*21}}},
 {'size':1,'query':{'ids':{'values':['a']}},'_source':['text.vector']},
 {'size':1,'query':{'ids':{'values':['a']}},'knn':[]},
])
def test_malicious_or_excessive_requests_never_reach_elasticsearch(extra):
    calls=[]
    with TestClient(create_app(make_archive(lambda r:calls.append(r)))) as client:
        assert client.post(URL,json={'size':1,**extra}).status_code==400
    assert not calls


@pytest.mark.parametrize('suffix',['?scroll=1h','?preference=mine','?scroll=1s&preference=archive-captures','?preference=archive-captures&preference=archive-captures'])
def test_scroll_and_unrecognized_parameters_are_not_forwarded(suffix):
    with TestClient(create_app(make_archive(lambda _:pytest.fail('upstream called')))) as client:
        assert client.post(URL+suffix,json={'size':0}).status_code==400


def test_deep_queries_node_budget_duplicate_keys_nonfinite_and_body_bounds():
    deep={'match_all':{}}
    for _ in range(10):deep={'bool':{'filter':[deep]}}
    b=Builder();b.nodes=160
    with pytest.raises(ValueError):b.query({'match_all':{}})
    with TestClient(create_app(make_archive(lambda _:pytest.fail('upstream called')))) as client:
        for body in [json.dumps({'query':deep}),'{"size":1,"size":0}','{"x":NaN}','['*2000, 'a'*(MAX_BODY+1)]:
            assert client.post(URL,content=body,headers={'Content-Type':'application/json'}).status_code==400
        assert client.post(URL,content='{}').status_code==400
        assert client.post(URL,content=b'\xff',headers={'Content-Type':'application/json'}).status_code==400


def test_filters_facets_recent_sites_reader_and_capture_history_remain_available():
    b=Builder()
    filters={'bool':{'must':[{'terms':{'domain_name':['example.org']}}],'must_not':[{'exists':{'field':'id'}}],
        'filter':[{'nested':{'path':'llm_extracted_dates','query':{'range':{'llm_extracted_dates.date':{'gte':'1999-01-01','lte':'2001-12-31'}}}}}],
        'should':[{'query_string':{'query':'"ancient cyclops" AND rings OR NOT cleric','fields':['text_full','llm_summary','llm_image_text'],'default_operator':'AND'}}]}}
    aggs={'facet_bucket_all':{'filter':{'bool':{'must':[]}},'aggs':{f:{'terms':{'field':f,'size':100,'order':{'_count':'desc'}}} for f in FACETS}}}
    aggs['facet_bucket_all']['aggs']['last_indexed']={'filters':{'filters':{'Today':{'range':{'last_indexed':{'gte':'2026-01-01T00:00:00Z'}}}}}}
    body=b.search({'query':filters,'post_filter':{'term':{'file_type':'text'}},'aggs':aggs,'knn':[]},'model')
    q=body['query']['bool']['should'][0]['simple_query_string']
    assert 'PREFIX' not in q['flags'] and q['query']=='"ancient cyclops" + rings | -cleric'
    assert body['post_filter']=={'term':{'file_type':'text'}}
    assert len(body['aggs']['facet_bucket_all']['aggs'])==7
    assert Builder().search({'size':1,'query':{'ids':{'values':['a']}},'_source':['text_full','discussion']},'model')['_source']==['text_full','discussion']
    assert Builder().search({'size':0,'_source':False},'model')['_source'] is False
    capture={'regexp':{'id':r'websites/example\.org/[0-9]{14}/a\?b=1'}}
    assert Builder().query(capture)['regexp']['id']['max_determinized_states']==1000
    assert Builder().query({'match_phrase':{'url':'http://example.org/a'}})=={'match_phrase':{'url':'http://example.org/a'}}
    recent={'sites':{'terms':{'field':'domain_name','size':50,'order':[{'latest_indexed':'desc'},{'_key':'asc'}]},'aggs':{'latest_indexed':{'max':{'field':'last_indexed'}},'first_capture':{'min':{'field':'capture_date'}},'last_capture':{'max':{'field':'capture_date'}}}}}
    assert Builder().search({'size':0,'aggs':recent,'query':{'bool':{'filter':[{'prefix':{'id':'websites/'}},{'exists':{'field':'last_indexed'}}]}}},'model')['aggs']==recent
    with pytest.raises(ValueError):Builder().aggs({'sites':{}})
    with pytest.raises(ValueError):Builder().aggs({'facet_bucket_all':{'filter':{'match_all':{}},'aggs':{'domain_name':{'terms':{'field':'domain_name','order':{'_key':'desc'}}}}}})


@pytest.mark.parametrize('change',[
 {'field':'arbitrary'}, {'k':101}, {'num_candidates':501}, {'query_vector':[0]*768},
 {'query_vector':[float('nan')]*768},{'query_vector':[1]*767},{'query_vector':[True]*768},
])
def test_vector_work_is_bounded_and_finite(change):
    branch={'field':'text.vector','k':50,'num_candidates':250,'query_vector':VECTOR,**change}
    with pytest.raises(ValueError):Builder().search({'knn':[branch]},'model')


def test_calibrated_source_leg_accepts_100_neighbors_without_relaying_caller_boost():
    branch={'field':'text.vector','k':100,'num_candidates':500,'query_vector':VECTOR,
            'boost':1000000,'filter':{'term':{'domain_name':'example.org'}}}
    compiled=Builder().search({'knn':[branch]},'gemma')['knn'][0]
    assert compiled['k']==100 and compiled['num_candidates']==500 and compiled['boost']==10
    assert compiled['filter']==[{'term':{'domain_name':'example.org'}},{'term':{'embedding_model':'gemma'}}]
    with pytest.raises(ValueError):Builder().search({'knn':[{**branch,'num_candidates':99}]},'gemma')


def test_vector_only_search_never_adds_match_all_and_supported_page_sizes_fit_window():
    body=Builder().search({'size':1,'_source':False,'knn':{
        'field':'llm_summary_vector','k':1,'num_candidates':10,'query_vector':VECTOR}},'gemma')
    assert 'query' not in body and body['knn'][0]['filter']==[{'term':{'embedding_model':'gemma'}}]
    assert Builder().search({'size':60},'gemma')['size']==60
    assert Builder().search({'size':60,'from':960},'gemma')['size']==40
    assert Builder().search({'knn':[]},'gemma')['query']=={'match_all':{}}


def test_embedding_endpoint_accepts_only_one_bounded_query_and_shares_cache():
    seen=[]
    def embed(req):
        seen.append(json.loads(req.content));return httpx.Response(200,json={'data':[{'embedding':VECTOR}]})
    with TestClient(create_app(make_archive(embedding_handler=embed))) as client:
        body={'model':'embeddinggemma-2-768-v1','input':['task: search result | query: cyclops'],'encoding_format':'float'}
        for _ in range(2):
            response=client.post('/openai/v1/embeddings',json=body)
            assert response.status_code==200 and response.json()['data'][0]['embedding']==VECTOR
        for change in ({'input':body['input']*2},{'input':['x']},{'input':['task: search result | query: '+'x'*601]}, {'model':'arbitrary'},{'encoding_format':'base64'},{'dimensions':1},{'input':None}):
            assert client.post('/openai/v1/embeddings',json={**body,**change}).status_code==400
    assert len(seen)==1 and seen[0]['input']==['task: search result | query: cyclops']


def test_embedding_busy_and_upstream_failures_are_sanitized():
    archive=make_archive(embedding_handler=lambda _:httpx.Response(500,text='private credential'))
    with TestClient(create_app(archive)) as client:
        response=client.post('/openai/v1/embeddings',json={'model':archive.settings.model,'input':'task: search result | query: a'})
        assert response.status_code==503 and 'private' not in response.text


def test_count_is_cached_routes_are_exact_and_host_origin_are_validated():
    calls=[]
    def es(request):
        calls.append(request);return httpx.Response(200,json={'hits':{'hits':[],'total':{'value':42,'relation':'eq'}}})
    with TestClient(create_app(make_archive(es))) as client:
        for _ in range(2):assert client.get('/elasticsearch/eq-archive/_count').json()=={'count':42}
        assert client.get('/elasticsearch/eq-archive/_count?x=1').status_code==400
        assert client.request('GET','/elasticsearch/eq-archive/_count',content=b'x').status_code==400
        assert client.post(URL,json={},headers={'Host':'evil.test'}).status_code==421
        assert client.post(URL,json={},headers={'Origin':'https://evil.test'}).status_code==403
        assert client.post('/elasticsearch/other/_search',json={}).status_code==404
        assert client.post(URL+'/extra',json={}).status_code==404
        assert client.get(URL).status_code==405
        assert client.delete('/openai/v1/embeddings').status_code==405
    assert len(calls)==1


def test_concurrent_requests_reject_excess_work_without_queuing_and_recover():
    async def exercise():
        release=asyncio.Event();started=asyncio.Event();calls=[]
        async def es(req):
            calls.append(req)
            if len(calls)==2:started.set()
            await release.wait()
            return httpx.Response(200,json={'hits':{'hits':[]}})
        archive=make_archive(es)
        from eqarchives_mcp.web_api import WebAPI
        from starlette.requests import Request
        api=WebAPI(archive)
        def request():
            async def receive():return {'type':'http.request','body':b'{}','more_body':False}
            return Request({'type':'http','method':'POST','path':'/','query_string':b'','headers':[(b'content-type',b'application/json')]},receive)
        tasks=[asyncio.create_task(api.handle(request(),'search')) for _ in range(2)]
        await asyncio.wait_for(started.wait(),1)
        assert (await api.handle(request(),'search')).status_code==503
        release.set();assert all(r.status_code==200 for r in await asyncio.gather(*tasks))
        assert (await api.handle(request(),'search')).status_code==200
        async with archive.requests,archive.requests,archive.requests,archive.requests:
            assert (await api.handle(request(),'search')).status_code==503
        await archive.close()
    asyncio.run(exercise())


def test_timeout_releases_admission_slots_without_replaying_work(monkeypatch):
    from eqarchives_mcp import web_api
    original=asyncio.timeout
    monkeypatch.setattr(web_api.asyncio,'timeout',lambda _:original(0.01))
    async def es(req):await asyncio.sleep(1)
    with TestClient(create_app(make_archive(es))) as client:
        response=client.post(URL,json={})
        assert response.status_code==503 and response.headers['retry-after']=='5'


def test_query_cost_budget_applies_across_clauses_and_facets_and_preserves_quotes():
    clause={'query_string':{'query':'"Rage AND Fury" OR cleric','fields':['text_full','llm_summary','llm_image_text']}}
    assert Builder().query(clause)['simple_query_string']['query']=='"Rage AND Fury" | cleric'
    with pytest.raises(ValueError):Builder().query({'bool':{'should':[clause]*9}})
    clause['query_string']['query']='x'*1000
    with pytest.raises(ValueError):Builder().query({'bool':{'should':[clause]*5}})
    aggs={name:{'filter':{'match_all':{}},'aggs':{f:{'terms':{'field':f,'size':100}} for f in FACETS}} for name in ('facet_bucket_all','facet_bucket_domain_name')}
    with pytest.raises(ValueError):Builder().aggs(aggs)


def test_slow_response_consumers_cannot_bypass_global_admission_and_cancellation_recovers():
    from eqarchives_mcp.web_api import WebAdmission
    async def exercise():
        entered=asyncio.Event();release=asyncio.Event();counter=[];rejected=[]
        async def app(scope,receive,send):
            counter.append(scope)
            if len(counter)==2:entered.set()
            await send({'type':'http.response.start','status':200,'headers':[]})
            await release.wait()
            await send({'type':'http.response.body','body':b'{}'})
        guard=WebAdmission(app)
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):pass
        async def capture(message):rejected.append(message)
        scope={'type':'http','path':URL}
        tasks=[asyncio.create_task(guard(scope,receive,send)) for _ in range(2)]
        await entered.wait()
        await guard(scope,receive,capture)
        assert rejected[0]['status']==503 and len(counter)==2
        tasks[0].cancel();await asyncio.gather(tasks[0],return_exceptions=True)
        release.set();await tasks[1]
        await guard(scope,receive,send)
        assert len(counter)==3
    asyncio.run(exercise())


def test_admission_deadline_closes_partial_transfers_and_releases_capacity(monkeypatch):
    from eqarchives_mcp import web_api
    original=asyncio.timeout
    monkeypatch.setattr(web_api.asyncio,'timeout',lambda _:original(.01))
    async def exercise(start):
        async def app(scope,receive,send):
            if start:await send({'type':'http.response.start','status':200,'headers':[]})
            await asyncio.sleep(1)
        guard=web_api.WebAdmission(app);sent=[]
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):sent.append(message)
        if start:
            with pytest.raises(TimeoutError):await guard({'type':'http','path':URL},receive,send)
        else:
            await guard({'type':'http','path':URL},receive,send)
            assert sent[0]['status']==503
        assert not guard.slots.locked()
    asyncio.run(exercise(False));asyncio.run(exercise(True))


@pytest.mark.parametrize('source', [[], ['title'] * 1000])
def test_reader_rejects_empty_and_oversized_source_selections_before_es(source):
    calls=[]
    with TestClient(create_app(make_archive(lambda request:calls.append(request)))) as client:
        response=client.post(URL,json={'size':1,'query':{'ids':{'values':['synthetic-id']}},'_source':source})
        assert response.status_code==400
    assert not calls


@pytest.mark.parametrize('kind,value', [
    ('match_phrase', 'a '*101),
    ('match_phrase', 'a/'*101),
    ('multi_match', 'a/'*101),
    ('query_string', 'a/'*101),
    ('multi_match', '漢'*101),
])
def test_analyzed_text_limits_include_url_phrases_punctuation_and_unicode(kind,value):
    clause={kind:{'url':value}} if kind=='match_phrase' else {kind:{'query':value,'fields':TEXT,**({'type':'best_fields'} if kind=='multi_match' else {})}}
    calls=[]
    with TestClient(create_app(make_archive(lambda request:calls.append(request)))) as client:
        assert client.post(URL,json={'size':0,'query':clause}).status_code==400
    assert not calls


def test_url_phrases_share_cost_budget_across_query_filters_facets_and_knn():
    url={'match_phrase':{'url':'http://example.org/quest.html'}}
    body={'size':0,'query':{'bool':{'filter':[url]*2}},'post_filter':url,
          'aggs':{'facet_bucket_all':{'filter':url,'aggs':{}}},
          'knn':{'field':'text.vector','k':1,'num_candidates':1,'query_vector':VECTOR,'filter':[url]*5}}
    calls=[]
    def es(request):
        calls.append(json.loads(request.content));return httpx.Response(200,json={'hits':{'hits':[]}})
    with TestClient(create_app(make_archive(es))) as client:
        assert client.post(URL,json=body).status_code==400
        assert not calls
        body['knn']['filter'].pop()
        assert client.post(URL,json=body).status_code==200
    assert len(calls)==1 and calls[0]['post_filter']==url


def test_many_url_phrase_words_cannot_bypass_the_shared_budget():
    url={'match_phrase':{'url':'a '*500}}
    body={'size':0,'query':{'bool':{'should':[{'bool':{'should':[url]*20}}]*4}}}
    assert len(json.dumps(body).encode())<MAX_BODY
    calls=[]
    with TestClient(create_app(make_archive(lambda request:calls.append(request)))) as client:
        assert client.post(URL,json=body).status_code==400
    assert not calls


def test_analyzed_text_budget_preserves_regular_queries_and_long_ascii_words():
    for query in ('"ancient cyclops" AND cleric', '戦士の叙事詩', 'x'*1000):
        clause={'multi_match':{'query':query,'fields':TEXT,'type':'best_fields'}}
        compiled=Builder().search({'query':clause},'gemma')
        assert compiled['query']['multi_match']['query']==query


@pytest.mark.parametrize('value', [10**1000,-10**1000,float('inf'),float('-inf'),float('nan'),True])
def test_bad_vector_numbers_return_400_without_upstream_work(value):
    vector=VECTOR.copy();vector[0]=value
    calls=[]
    with TestClient(create_app(make_archive(lambda request:calls.append(request))),raise_server_exceptions=False) as client:
        body={'size':0,'knn':{'field':'text.vector','k':1,'num_candidates':1,'query_vector':vector}}
        response=client.post(URL,content=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        assert response.status_code==400
    assert not calls


@pytest.mark.parametrize('invalid', ['\ud800','\udfff','cleric\ud800quest'])
def test_malformed_unicode_cannot_poison_shared_embedding_availability(invalid):
    calls=[]
    def embed(request):
        calls.append(json.loads(request.content));return httpx.Response(200,json={'data':[{'embedding':VECTOR}]})
    archive=make_archive(embedding_handler=embed)
    with TestClient(create_app(archive),raise_server_exceptions=False) as client:
        body={'model':archive.settings.model,'input':'task: search result | query: '+invalid}
        response=client.post('/openai/v1/embeddings',content=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        assert response.status_code==400
        assert archive.cooldown_until==0 and not calls
        body['input']='task: search result | query: cleric epic 😀'
        assert client.post('/openai/v1/embeddings',json=body).status_code==200
    assert len(calls)==1


def test_invalid_unicode_in_search_is_rejected_without_es_work():
    calls=[]
    with TestClient(create_app(make_archive(lambda request:calls.append(request)))) as client:
        body={'query':{'term':{'id':'bad\ud800id'}}}
        assert client.post(URL,content=json.dumps(body).encode(),headers={'Content-Type':'application/json'}).status_code==400
    assert not calls
