import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from eqarchives_mcp.rerank import Reranker, MODEL, FIELDS, PREFIX, SUFFIX, passage, scores
from eqarchives_mcp.ranking_policy import RERANK_WINDOW
from eqarchives_mcp.passages import PASSAGE_FIELDS, FALLBACK_FIELDS
from eqarchives_mcp.server import create_app
from test_archive import make_archive, run

URL = '/elasticsearch/eq-archive/_rerank'

class Tokens:
    def encode(self, text, **kwargs): return SimpleNamespace(ids=list(map(ord, text)))
    def decode(self, ids): return ''.join(map(chr, ids))

def scorer(handler=None):
    def normal(request):
        payload = json.loads(request.content)
        numeric = all(d.rsplit(' ',1)[-1].isdigit() for d in payload['documents'])
        return httpx.Response(200, json={'model': MODEL, 'results': [
            {'index': n, 'relevance_score': int(d.rsplit(' ',1)[-1])/100 if numeric else (n+1)/len(payload['documents'])} for n,d in enumerate(payload['documents'])]})
    return Reranker(httpx.AsyncClient(base_url='http://reranker/', transport=httpx.MockTransport(handler or normal)), Tokens())

def fixture_archive(handler=None):
    calls = []
    def es(request):
        body = json.loads(request.content); calls.append(body)
        ids = body.get('query', {}).get('ids', {}).get('values')
        if body.get('_source') == ['title']:
            ids = body['query']['bool']['filter'][0]['ids']['values']
            hits = [{'_id': id, '_source': {'title': 'EverQuest'}, 'inner_hits': {
                'lexical_passage': {'hits': {'hits': [{'_nested': {'field':'text','offset':3},
                  '_source': {'text_chunk': 'EverQuest bard twisting ' + id}}]}}}}
                    for id in ids if id != 'image']
            return httpx.Response(200, json={'hits': {'hits': hits}})
        if ids is not None:
            assert body['_source'] in (FIELDS, FALLBACK_FIELDS) and len(ids) <= 60
            hits = [{'_id': id, '_source': {'text': [{'text_chunk': 'EverQuest bard twisting ' + id}]}} for id in ids if id != 'image']
        else:
            hits = [{'_id': str(n), '_source': {'id': str(n), 'title': str(n)}} for n in range(80)]
            hits = hits[body.get('from', 0):body.get('from', 0)+body['size']]
        return httpx.Response(200, json={'hits': {'hits': hits, 'total': {'value': 80, 'relation': 'eq'}}})
    archive = make_archive(es)
    archive.reranker = scorer(handler)
    return archive, calls

def test_select_passage_context_japanese_metadata_and_total_budget():
    source = {'title': 'Bard', 'text': [{'text_chunk': 'unrelated'}, {'text_chunk': 'twisting ' * 900, 'context': 'Author: Aria'}]}
    doc = passage('twisting', source, Tokens())
    assert doc.startswith('Bard\nAuthor: Aria\n\ntwisting')
    assert len(Tokens().encode(PREFIX+'twisting\n<Document>: '+doc+SUFFIX).ids) <= 768
    assert passage('x'*1000, source, Tokens()) is None
    assert passage('x', {'text': 'bad'}, Tokens()) is None
    assert passage('x', {'text': [{}]*4097}, Tokens()) is None
    assert passage('x', {}, Tokens()) is None
    assert '本文' in passage('本文', {'llm_image_text_full': '日本語本文'}, Tokens())
    assert passage('x', {'text_full': 'body'}, Tokens()) == 'body'

def test_real_pinned_tokenizer_keeps_selected_passage():
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file('/app/qwen-tokenizer.json')
    source = {'title': 'バード', 'text': [{'text_chunk': 'EverQuest song twisting and bard mechanics. ' * 80}]}
    doc = passage('bard twisting', source, tokenizer)
    assert len(tokenizer.encode(PREFIX+'bard twisting\n<Document>: '+doc+SUFFIX, add_special_tokens=False).ids) <= 768

@pytest.mark.parametrize('data', [None, {}, {'model': 'bad'}, {'model': MODEL, 'results': []},
    {'model': MODEL, 'results': [None]}, {'model': MODEL, 'results': [{'index': True, 'relevance_score': .5}]},
    {'model': MODEL, 'results': [{'index': 0, 'relevance_score': float('nan')}]},
    {'model': MODEL, 'results': [{'index': 0, 'relevance_score': True}]},
    {'model': MODEL, 'results': [{'index': 9, 'relevance_score': .2}]}])
def test_malformed_scores(data):
    with pytest.raises(ValueError): scores(data, 1)

def test_duplicate_score_indices():
    with pytest.raises(ValueError): scores({'model': MODEL, 'results': [{'index':0,'relevance_score':.5}]*2},2)

def test_order_only_moves_supported_head_and_reuses_order_without_source_reads():
    async def scenario():
        archive, calls = fixture_archive()
        ids, status = await archive.rank_ids('bard', ['a','image','b'])
        assert ids == ['b','image','a'] and status == 'applied'
        assert await archive.rank_ids('bard', ['a','image','b']) == (ids, status)
        assert len(calls) == 1
        archive.reranker.cache.clear()
        assert await archive.rank_ids('bard', ['image','missing']) == (['image','missing'], 'applied')
        await archive.close()
    run(scenario())

@pytest.mark.parametrize('failure', ['timeout','http','redirect','malformed','oversized'])
def test_failure_is_bounded_cached_and_does_not_break_search(failure):
    def handler(request):
        if failure == 'timeout': raise httpx.ReadTimeout('private credential')
        if failure == 'http': return httpx.Response(500,text='secret')
        if failure == 'redirect': return httpx.Response(302,headers={'Location':'http://evil/'})
        if failure == 'oversized': return httpx.Response(200,content=b'x'*32769)
        return httpx.Response(200,json={'model':MODEL,'results':[]})
    async def scenario():
        archive, calls = fixture_archive(handler)
        assert await archive.rank_ids('bard', ['a','b']) == (['a','b'], 'fallback')
        assert archive.reranker.cooldown > 0
        assert await archive.rank_ids('bard', ['a','b']) == (['a','b'], 'fallback')
        assert len(calls) == 1
        archive.reranker.cooldown = 0
        archive.reranker.cache.clear()
        archive.reranker.client = scorer().client
        assert await archive.rank_ids('bard', ['a','b']) == (['b','a'], 'applied')
        await archive.close()
    run(scenario())

def test_identical_queries_share_scoring_and_cancellation_releases_capacity():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(request):
            started.set(); await release.wait()
            return httpx.Response(200,json={'model':MODEL,'results':[{'index':0,'relevance_score':0},{'index':1,'relevance_score':1}]})
        archive, _ = fixture_archive(slow)
        first = asyncio.create_task(archive.rank_ids('bard',['a','b']))
        await started.wait()
        second = asyncio.create_task(archive.rank_ids('bard',['a','b']))
        await asyncio.sleep(0)
        release.set()
        assert await first == await second == (['b','a'],'applied')
        archive.reranker.cache.clear(); release.clear()
        task = asyncio.create_task(archive.rank_ids('bard',['a','b']))
        await asyncio.sleep(.01); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not archive.reranker.lock.locked()
        archive.reranker.cache.clear()
        archive.reranker.deadline = .01
        assert await archive.rank_ids('bard',['a','b']) == (['a','b'],'fallback')
        await archive.close()
    run(scenario())

@pytest.mark.parametrize('body', [ {'query':'bard','ids':['a']*2}, {'query':'bard','ids':[str(n) for n in range(61)]},
    {'query':'bard','ids':[]}, {'query':'bard','ids':[True]}, {'query':'x '*101,'ids':['a','b']},
    {'query':'\ud800','ids':['a','b']}, {'query':'bard','ids':['\ud800','b']},
    {'query':'bard','ids':['a','b'],'url':'http://evil/'}, {'query':'bard','ids':['a','b'],'script':'pwn'},
    {'query':'bard','ids':['a\n','b']}, {'query':'bard','ids':['x'*2049,'b']} ])
def test_public_contract_rejects_before_upstream(body):
    archive, calls = fixture_archive()
    with TestClient(create_app(archive)) as client:
        assert client.post(URL,content=json.dumps(body),headers={'Content-Type':'application/json'}).status_code == 400
    assert not calls

def test_public_endpoint_only_returns_exact_ids_and_safe_status():
    archive, _ = fixture_archive()
    with TestClient(create_app(archive)) as client:
        response = client.post(URL,json={'query':'bard','ids':['a','b']})
        assert response.json() == {'ids':['b','a'],'status':'applied'}
        assert client.get(URL).status_code == 405
        assert client.post(URL+'?url=evil',json={}).status_code == 400
        assert client.post(URL.replace('eq-archive','other'),json={}).status_code == 404

def test_mcp_search_and_research_pages_share_selected_head_without_duplicates_or_tail_changes():
    async def scenario():
        archive, calls = fixture_archive()
        found = await archive.search('bard')
        assert [r.id for r in found.results] == [str(n) for n in range(RERANK_WINDOW-1,RERANK_WINDOW-11,-1)]
        pages = [await archive.search_archive('bard', offset=offset, limit=min(10,RERANK_WINDOW+5-offset))
                 for offset in range(0,RERANK_WINDOW+5,10)]
        assert [r.id for page in pages for r in page.results] == [str(n) for n in range(RERANK_WINDOW-1,-1,-1)]+list(map(str,range(RERANK_WINDOW,RERANK_WINDOW+5)))
        before = len([c for c in calls if c['_source']==['title']])
        date = await archive.search_archive('bard', limit=10, sort='capture_date_desc')
        assert date.results[0].id == '0'
        assert len([c for c in calls if c['_source']==['title']]) == before
        grouped = await archive.search_archive('bard', limit=10, group_by='discussion')
        assert [r.id for r in grouped.results] == list(map(str,range(RERANK_WINDOW-1,RERANK_WINDOW-11,-1)))
        await archive.close()
    run(scenario())

def test_optional_configuration_and_checksum_failure(tmp_path, monkeypatch):
    monkeypatch.delenv('RERANK_URL',raising=False)
    assert Reranker.connect() is None
    monkeypatch.setenv('RERANK_URL','http://test/')
    path=tmp_path/'tokenizer'; path.write_text('bad')
    monkeypatch.setenv('RERANK_TOKENIZER',str(path))
    with pytest.raises(ValueError,match='checksum'): Reranker.connect()
    monkeypatch.setenv('RERANK_TOKENIZER','/app/qwen-tokenizer.json')
    key=tmp_path/'key';key.write_text('')
    monkeypatch.setenv('RERANK_KEY_FILE',str(key))
    with pytest.raises(ValueError,match='credential'): Reranker.connect()
    key.write_text('private-test')
    run(Reranker.connect().close())

def test_large_source_preparation_is_bounded_and_missing_sources_keep_slots():
    class BoundedTokens(Tokens):
        def encode(self, text, **kwargs):
            assert len(text) <= 8192
            return super().encode(text, **kwargs)
    assert passage('bard', {'title':'title '*50000,'text_full':'bard '*500000}, BoundedTokens())
    async def scenario():
        archive, _ = fixture_archive()
        async def empty(body): return {'hits': {'hits': []}}
        archive.execute = empty
        assert await archive.rank_ids('bard',['a','b']) == (['a','b'],'no_text')
        await archive.close()
    run(scenario())

def test_filtered_retrieval_stays_constrained_and_cache_expiry_allows_new_order():
    from eqarchives_mcp.research import ResearchFilters
    async def scenario():
        archive, calls = fixture_archive()
        await archive.search_archive('"bard" -cleric', filters=ResearchFilters(domains=['example.org'],capture_from='1999-01-01',capture_to='2001-12-31'))
        query = calls[0]['query']
        assert 'example.org' in json.dumps(query) and '1999-01-01' in json.dumps(query)
        assert query['bool']['must'][0]['simple_query_string']['query']=='"bard" -cleric'
        assert 'knn' not in calls[0]
        key=next(iter(archive.reranker.cache))
        _, order, status=archive.reranker.cache[key]
        archive.reranker.cache[key]=(0,order,status)
        count=len(calls)
        await archive.search_archive('"bard" -cleric', filters=ResearchFilters(domains=['example.org'],capture_from='1999-01-01',capture_to='2001-12-31'))
        assert len(calls)==count+2
        await archive.close()
    run(scenario())


def test_matching_passages_reuse_valid_query_vector_and_keep_attribution_and_ocr():
    import time
    from test_archive import VECTOR
    async def scenario():
        archive, _ = fixture_archive()
        archive.cache[archive.settings.model, 'bard']=(time.monotonic()+60,VECTOR)
        calls=[]
        async def es(body):
            calls.append(body)
            if body['_source']==FALLBACK_FIELDS:
                assert body['query']['bool']['filter'][0]['ids']['values']==['image']
                return {'hits':{'hits':[{'_id':'image','_source':{'title':'Sheet','llm_image_text_full':'song lyrics'}}]}}
            rows=[]
            for id in ('a','b','image'):
                h={'_id':id,'_source':{'title':'Bard'},'inner_hits':{}}
                if id!='image':
                    for name,body in [('semantic','Useful explanation'),('lexical','Alternative explanation')]:
                        h['inner_hits'][name+'_passage']={'hits':{'hits':[{'_nested':{'field':'text','offset':42},
                            '_source':{'text':{'text_chunk':body,'context':'Author: '+id}}}]}}
                rows.append(h)
            return {'hits':{'hits':rows}}
        archive.execute=es;archive.reranker.passage_mode='paired'
        inputs=await archive.reranker.prepare(archive,'bard',['a','b','image'])
        assert len(inputs)==5 and inputs[-1]==(2,'Sheet\n\nsong lyrics')
        assert inputs[0][1].startswith('Bard\nAuthor: a\n\nUseful')
        assert inputs[2][1].startswith('Bard\nAuthor: b\n\nUseful')
        assert calls[0]['knn']['query_vector']==VECTOR
        archive.cache[archive.settings.model,'bard']=(0,VECTOR)
        calls.clear();await archive.reranker.prepare(archive,'bard',['a','b','image'])
        assert 'knn' not in calls[0]
        await archive.close()
    run(scenario())


def test_scoring_deduplicates_complete_inputs_batches_and_combines_two_passages():
    calls=[]
    def handler(request):
        payload=json.loads(request.content);calls.append(payload)
        assert len(request.content)<=128*1024 and len(payload['documents'])<=20
        return httpx.Response(200,json={'model':MODEL,'results':[
            {'index':n,'relevance_score':int(d.split(':')[0])/120} for n,d in enumerate(payload['documents'])]})
    async def scenario():
        rank=scorer(handler)
        inputs=[(n//2,f'{n}:body') for n in range(120)]+[(60,'119:body')]
        result=await rank.score('bard',inputs)
        assert [len(c['documents']) for c in calls]==[20]*6
        assert result[0]==1/120 and result[59]==result[60]==119/120
        with pytest.raises(ValueError,match='bound'):
            await rank.score('bard',[(n,str(n)) for n in range(121)])
        with pytest.raises(ValueError,match='large'):
            await rank.score('bard',[(0,'x'*140000)])
        calls.clear()
        await rank.score('bard',[(n,f'{n}:'+('x'*8000)) for n in range(40)])
        assert len(calls)==4
        await rank.close()
    run(scenario())


def test_failure_in_any_concurrent_scoring_batch_keeps_entire_original_order():
    calls=[]
    def handler(request):
        calls.append(request)
        size=len(json.loads(request.content)['documents'])
        return httpx.Response(200,json={'model':MODEL,'results':[
            {'index':n,'relevance_score':n/size} for n in range(size)]}) if len(calls)==1 else httpx.Response(503)
    async def scenario():
        archive,_=fixture_archive(handler)
        ids=[str(n) for n in range(60)]
        assert await archive.rank_ids('bard',ids)==(ids,'fallback')
        assert len(calls)==3
        await archive.close()
    run(scenario())


def test_legacy_control_and_matching_modes_have_independent_cached_orders():
    async def scenario():
        archive,calls=fixture_archive()
        archive.reranker.passage_mode='legacy'
        assert await archive.rank_ids('bard',['a','b'])==(['b','a'],'applied')
        assert calls[0]['_source']==FIELDS
        archive.reranker.passage_mode='semantic'
        assert await archive.rank_ids('bard',['a','b'])==(['b','a'],'applied')
        assert calls[-1]['_source']==['title'] and len(calls)==2
        await archive.close()
    run(scenario())


def test_runtime_batches_overlap_with_a_shared_four_request_limit():
    async def scenario():
        ready,release=asyncio.Event(),asyncio.Event()
        active=maximum=0
        async def handler(request):
            nonlocal active,maximum
            active+=1;maximum=max(active,maximum)
            if active==4:ready.set()
            try:
                await release.wait()
                docs=json.loads(request.content)['documents']
                return httpx.Response(200,json={'model':MODEL,'results':[
                    {'index':n,'relevance_score':int(d)/120} for n,d in enumerate(docs)]})
            finally:active-=1
        rank=scorer(handler)
        task=asyncio.create_task(rank.score('bard',[(n,str(n)) for n in range(120)]))
        await asyncio.wait_for(ready.wait(),1)
        assert active==4 and not task.done()
        release.set();values=await task
        assert maximum==4 and active==0 and values[119]==119/120
        await rank.close()
    run(scenario())


def test_two_distinct_searches_score_concurrently_and_overload_is_bounded():
    async def scenario():
        ready,release=asyncio.Event(),asyncio.Event();calls=[]
        async def handler(request):
            calls.append(json.loads(request.content)['query'])
            if len(calls)==2:ready.set()
            await release.wait()
            return httpx.Response(200,json={'model':MODEL,'results':[
                {'index':0,'relevance_score':0},{'index':1,'relevance_score':1}]})
        archive,_=fixture_archive(handler)
        tasks=[asyncio.create_task(archive.rank_ids(q,['a','b'])) for q in ('bard','cleric')]
        await asyncio.wait_for(ready.wait(),1)
        assert await archive.rank_ids('wizard',['a','b'])==(['a','b'],'fallback')
        assert set(calls)=={'bard','cleric'}
        release.set()
        assert await asyncio.gather(*tasks)==[(['b','a'],'applied')]*2
        assert not archive.reranker.pending and not archive.reranker.lock.locked()
        await archive.close()
    run(scenario())


def test_coalesced_waiter_cancellation_does_not_cancel_owner():
    async def scenario():
        ready,release=asyncio.Event(),asyncio.Event();count=0
        async def handler(request):
            nonlocal count
            count+=1;ready.set();await release.wait()
            return httpx.Response(200,json={'model':MODEL,'results':[
                {'index':0,'relevance_score':0},{'index':1,'relevance_score':1}]})
        archive,_=fixture_archive(handler)
        owner=asyncio.create_task(archive.rank_ids('bard',['a','b']))
        await ready.wait()
        waiter=asyncio.create_task(archive.rank_ids('bard',['a','b']));await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):await waiter
        assert not owner.done()
        release.set();assert await owner==(['b','a'],'applied')
        assert count==1
        await archive.close()
    run(scenario())


def test_failed_batch_cancels_inflight_siblings_before_releasing_capacity():
    async def scenario():
        ready=asyncio.Event();active=0
        async def handler(request):
            nonlocal active
            active+=1
            try:
                docs=json.loads(request.content)['documents']
                if docs[0]=='0':
                    await ready.wait();return httpx.Response(503)
                ready.set();await asyncio.Event().wait()
            finally:active-=1
        rank=scorer(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await rank.score('bard',[(n,str(n)) for n in range(60)])
        assert active==0 and not rank.batch_slots.locked()
        await rank.close()
    run(scenario())


def test_explicit_keyword_research_retains_legacy_top20_and_paging():
    async def scenario():
        archive,calls=fixture_archive()
        page=await archive.search_archive('bard',limit=25,hybrid=False)
        assert [r.id for r in page.results]==list(map(str,range(19,-1,-1)))+list(map(str,range(20,25)))
        assert calls[1]['_source']==FIELDS and len(calls[1]['query']['ids']['values'])==20
        grouped=await archive.search_archive('bard',group_by='discussion',limit=25,hybrid=False)
        assert [r.id for r in grouped.results]==[r.id for r in page.results]
        assert 'knn' not in calls[0]
        await archive.close()
    run(scenario())
