import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from eqarchives_mcp.rerank import Reranker, MODEL, FIELDS, PREFIX, SUFFIX, passage, scores
from eqarchives_mcp.server import create_app
from test_archive import make_archive, run

URL = '/elasticsearch/eq-archive/_rerank'

class Tokens:
    def encode(self, text, **kwargs): return SimpleNamespace(ids=list(map(ord, text)))
    def decode(self, ids): return ''.join(map(chr, ids))

def scorer(handler=None):
    def normal(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={'model': MODEL, 'results': [
            {'index': n, 'relevance_score': (n+1)/len(payload['documents'])} for n in range(len(payload['documents']))]})
    return Reranker(httpx.AsyncClient(base_url='http://reranker/', transport=httpx.MockTransport(handler or normal)), Tokens())

def fixture_archive(handler=None):
    calls = []
    def es(request):
        body = json.loads(request.content); calls.append(body)
        ids = body.get('query', {}).get('ids', {}).get('values')
        if ids is not None:
            assert body['_source'] == FIELDS and len(ids) <= 20
            hits = [{'_id': id, '_source': {'text': [{'text_chunk': 'EverQuest bard twisting ' + id}]}} for id in ids if id != 'image']
        else:
            hits = [{'_id': str(n), '_source': {'id': str(n), 'title': str(n)}} for n in range(30)]
            hits = hits[body.get('from', 0):body.get('from', 0)+body['size']]
        return httpx.Response(200, json={'hits': {'hits': hits, 'total': {'value': 30, 'relation': 'eq'}}})
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

def test_busy_same_query_cannot_install_two_different_paging_orders_and_cancellation_releases():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(request):
            started.set(); await release.wait()
            return httpx.Response(200,json={'model':MODEL,'results':[{'index':0,'relevance_score':0},{'index':1,'relevance_score':1}]})
        archive, _ = fixture_archive(slow)
        first = asyncio.create_task(archive.rank_ids('bard',['a','b']))
        await started.wait()
        second = await archive.rank_ids('bard',['a','b'])
        release.set()
        assert await first == second == (['a','b'],'fallback')
        archive.reranker.cache.clear(); release.clear()
        task = asyncio.create_task(archive.rank_ids('bard',['a','b']))
        await asyncio.sleep(.01); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not archive.reranker.lock.locked()
        archive.reranker.deadline = .01
        assert await archive.rank_ids('bard',['a','b']) == (['a','b'],'fallback')
        await archive.close()
    run(scenario())

@pytest.mark.parametrize('body', [ {'query':'bard','ids':['a']*2}, {'query':'bard','ids':['x']*21},
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

def test_mcp_search_and_research_pages_share_top20_without_duplicates_or_tail_changes():
    async def scenario():
        archive, calls = fixture_archive()
        found = await archive.search('bard')
        assert [r.id for r in found.results] == [str(n) for n in range(19,9,-1)]
        first = await archive.search_archive('bard', limit=10)
        second = await archive.search_archive('bard', offset=10, limit=15)
        assert [r.id for r in first.results+second.results] == [str(n) for n in range(19,-1,-1)]+list(map(str,range(20,25)))
        before = len([c for c in calls if c['_source']==FIELDS])
        date = await archive.search_archive('bard', limit=10, sort='capture_date_desc')
        assert date.results[0].id == '0'
        assert len([c for c in calls if c['_source']==FIELDS]) == before
        grouped = await archive.search_archive('bard', limit=10, group_by='discussion')
        assert [r.id for r in grouped.results] == list(map(str,range(19,9,-1)))
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
