import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from eqarchives_mcp.explore import (
    Explorer, Selection, overview_query, phrases_query, extract_phrases, page_identity, parse_overview,
)
from eqarchives_mcp.server import create_app
from test_archive import make_archive


def overview():
    return {'hits': {'total': {'value': 12, 'relation': 'eq'}, 'hits': []}, 'aggregations': {
        'sites_count': {'value': 2}, 'tagged': {'doc_count': 6},
        'themes': {'buckets': [{'key': 'raid', 'doc_count': 4, 'doc_count_error_upper_bound': 0}]},
        'sites': {'buckets': [{'key': 'guild.org', 'doc_count': 10, 'doc_count_error_upper_bound': 2}, {'key': '', 'doc_count': 2}]},
        'timeline': {'buckets': [{'key_as_string': '2000', 'doc_count': 12, 'sites': {'value': 2}}]},
    }}


def hit(path='one', text='Ancient cyclops guards the desert. Rare ring hunters gather.', domain='guild.org', stamp='20000101000000'):
    identifier = f'websites/{domain}/{stamp}/{path}'
    return {'_id': identifier, '_source': {'id': identifier, 'domain_name': domain}, 'fields': {'excerpt': [text]}}


def sample(hits=None):
    return {'hits': {'hits': []}, 'aggregations': {'sample': {'pages': {'hits': {'hits': hits or [hit()]}}}}}


@pytest.mark.parametrize('kwargs', [
    {'start': '2001-02-29'}, {'start': '2002-01-01', 'end': '2001-01-01'},
    {'start': '1989-12-31'}, {'end': '2100-01-01'}, {'end': '2050-01-01'},
    {'basis': 'last_indexed'}, {'query': {'match_all': {}}}, {'site': 'x' * 256},
    {'theme': '\nraid'}, {'phrase': 'x' * 101},
])
def test_rejects_unbounded_or_invalid_selections(kwargs):
    with pytest.raises(ValueError):
        Selection(**kwargs)


def test_queries_preserve_exact_scope_and_utc_dates_without_vectors_or_writes():
    selection = Selection(start='2000-02-29', end='2001-01-01', basis='llm_guessed_date',
                          site='www.guild.org:8080', theme='raid', phrase='ancient cyclops')
    body = overview_query(selection)
    assert body['size'] == 0 and body['_source'] is False
    assert body['track_total_hits'] is True and body['timeout'] == '8s'
    assert body['query']['bool']['filter'] == [
        {'prefix': {'id': 'websites/'}},
        {'range': {'llm_guessed_date': {'gte': '2000-02-29T00:00:00.000Z', 'lte': '2001-01-01T23:59:59.999Z'}}},
        {'term': {'llm_tags': 'raid'}}, {'term': {'domain_name': 'www.guild.org:8080'}},
        {'match_phrase': {'text_full': 'ancient cyclops'}},
    ]
    assert body['aggs']['timeline']['date_histogram']['field'] == 'llm_guessed_date'
    cloud = phrases_query(selection)
    assert cloud['query']['function_score']['query']['bool']['filter'][:-1] == body['query']['bool']['filter']
    sampler = cloud['aggs']['sample']
    assert sampler['diversified_sampler']['max_docs_per_value'] == 100
    pages = sampler['aggs']['pages']['top_hits']
    assert pages['size'] == 100
    assert 'text_full' not in pages['_source']
    assert pages['script_fields']['excerpt']['script']['params']['limit'] == 12000
    assert phrases_query(Selection())['aggs']['sample']['diversified_sampler']['max_docs_per_value'] == 8
    assert 'knn' not in cloud


def test_original_page_identity_preserves_ports_accounts_paths_queries_and_protocol():
    a = hit('Account/Path?a=1')
    b = hit('Account/Path?a=1', stamp='20010101000000')
    assert page_identity(a['_source'], a['_id']) == page_identity(b['_source'], b['_id'])
    original = page_identity(a['_source'], a['_id'])
    for path in ('Other/Path?a=1', 'Account/path?a=1', 'Account/Path?a=2'):
        other = hit(path)
        assert page_identity(other['_source'], other['_id']) != original
    source = {'url': 'https://web.archive.org/web/20000101000000id_/http://GUILD.org:80/Account/Path?a=1#fragment'}
    assert page_identity(source, 'opaque') == original
    source['url'] = source['url'].replace(':80', ':8080')
    assert ':8080/' in page_identity(source, 'opaque')
    source['url'] = source['url'].replace('/http:', '/https:')
    assert page_identity(source, 'opaque').startswith('https:')
    for url in ('https://web.archive.org/web/20000101000000/http://x:bad/path',
                'https://web.archive.org/web/20000101000000/http://u:pw@x/path', 'invalid'):
        assert page_identity({'url': url}, 'opaque') == 'opaque'
    assert page_identity({'parent_id': 'parent', **a['_source']}, 'child') == 'child'


def test_cloud_counts_pages_not_word_repetitions_and_removes_duplicates_and_navigation():
    hits = [hit('one', 'Home | Contact | Privacy\nAncient cyclops ancient cyclops'),
            hit('one', 'Changed ancient cyclops', stamp='20010101000000'),
            hit('mirror', 'Home | Contact | Privacy\nAncient cyclops ancient cyclops'),
            hit('two', 'Home | Contact | Privacy\nAn ancient cyclops drops a ring.', domain='other.org')]
    result = extract_phrases(hits)
    assert result['sampled_pages'] == 2 and result['duplicate_captures'] == 2
    assert {'text': 'ancient cyclops', 'pages': 2} in result['phrases']
    assert not any(word['text'] in ('home', 'contact', 'privacy') for word in result['phrases'])
    assert result['sampled_sites'] == 2
    assert 'text_full' not in json.dumps(result) and 'Home' not in json.dumps(result)


def test_cloud_bounds_removes_repeated_boilerplate_without_creating_cross_line_phrases():
    hits = [hit(str(i), f'Mysterious branding\nAncient cyclops drops\nRare platinum rings {chr(97+i)}') for i in range(12)]
    hits += [hit('blank', ''), hit('nottext', 42), hit('huge', 'abcdefghijkl ' * 2000, 'big.org')]
    result = extract_phrases(hits)
    assert result['sampled_pages'] == 9 and result['clipped_pages'] == 1
    assert all('mysterious' not in p['text'] and 'drops rare' not in p['text'] for p in result['phrases'])
    assert extract_phrases(hits, site_selected=True)['sampled_pages'] == 13
    assert extract_phrases([])['phrases'] == []
    many = [hit(str(i), ' '.join(f'word{chr(97 + j // 26)}{chr(97 + j % 26)}' for j in range(100)) + str(i), f'd{i}.org') for i in range(110)]
    bounded = extract_phrases(many)
    assert bounded['sampled_pages'] == 100 and len(bounded['phrases']) == 40


def test_cloud_uses_searchable_source_words_without_markdown_destinations_or_joined_fragments():
    source = '''[Old Fashion...](/equipment/fashion/Fashion_Table.html)
[Luclin Fashion...](/equipment/fashion.html)
[Ancient cyclops](../bestiary/secret_slug?search=hidden_keyword) guards the desert.
[Rare platinum](https://example.org/invisible/path) rings.
[1]: /hidden_reference/lookup
Bronze 123 shield. Copper/sword. Silver_armor. Mithril99.
Golden, dragon. Weapon<b>damage</b>. Rune https://example.org/spells sorcery.
Diamond &amp; emerald. Massive strength. Guard's steel.
'''
    result = extract_phrases([hit(text=source)])
    phrases = {p['text'] for p in result['phrases']}
    assert {'ancient cyclops', 'rare platinum', 'massive strength', "guard's steel"} <= phrases
    assert not any(p in phrases for p in (
        'equipment fashion fashion', 'fashion', 'table', 'bestiary', 'secret', 'slug',
        'hidden', 'keyword', 'invisible', 'path', 'reference', 'lookup', 'amp',
        'bronze shield', 'copper sword', 'silver armor', 'mithril', 'golden dragon',
        'weapon damage', 'rune sorcery', 'diamond emerald',
    ))
    # Every offered phrase occurs literally in the source (apart from case and
    # whitespace), so source phrase filters do not lead to invented matches.
    normalized = ' '.join(source.lower().split())
    assert all(p in normalized for p in phrases)


def test_overview_requires_complete_counts_and_marks_approximate_terms():
    result = parse_overview(overview())
    assert result['records'] == 12 and result['tagged'] == 6
    assert result['sites'] == [{'key': 'guild.org', 'count': 10, 'approximate': True}]
    for mutation in (lambda d: d['hits']['total'].update(relation='gte'),
                     lambda d: d['aggregations']['tagged'].update(doc_count=-1),
                     lambda d: d['aggregations']['sites_count'].update(value=True)):
        broken = overview(); mutation(broken)
        with pytest.raises(ValueError): parse_overview(broken)


def test_cached_summaries_coalesce_requests_expire_and_evict_without_model_calls():
    calls = []
    def es(request):
        body = json.loads(request.content); calls.append(body)
        return httpx.Response(200, json=sample() if 'sample' in body['aggs'] else overview())
    def no_model(request): pytest.fail('Explore must never request embeddings or AI')
    async def exercise():
        explorer = Explorer(make_archive(es, no_model))
        a, b = await asyncio.gather(explorer.get('overview', Selection()), explorer.get('overview', Selection()))
        assert a == b and len(calls) == 1
        assert await explorer.get('overview', Selection()) == a
        assert len(calls) == 1
        cloud = await explorer.get('phrases', Selection())
        assert cloud['sampled_pages'] == 1 and cloud['generated_at']
        assert cloud['selection']['start'] == '1999-01-01'
        with patch('eqarchives_mcp.explore.CACHE_ENTRIES', 2):
            await explorer.get('overview', Selection(theme='raid'))
        assert len(explorer.cache) == 2
        # Force expiry without sleeping.
        for key, (_, value) in list(explorer.cache.items()): explorer.cache[key] = (0, value)
        await explorer.get('overview', Selection(theme='raid'))
        assert len(calls) == 4
    asyncio.run(exercise())


def test_exploration_errors_are_safe_and_not_cached():
    async def exercise():
        explorer = Explorer(make_archive(lambda r: httpx.Response(200, json={'hits': {'hits': []}})))
        with pytest.raises(ToolError): await explorer.get('overview', Selection())
        assert not explorer.cache
        with patch.object(explorer.archive, 'requests', asyncio.Semaphore(0)):
            with pytest.raises(ToolError, match='busy'): await explorer.get('phrases', Selection())
    asyncio.run(exercise())


def test_http_explore_routes_validation_cache_security_and_failure_isolation():
    calls = []
    def es(request):
        body = json.loads(request.content); calls.append(body)
        return httpx.Response(200, json=sample() if 'sample' in body['aggs'] else overview())
    with TestClient(create_app(make_archive(es))) as client:
        for kind in ('overview', 'phrases'):
            response = client.get('/api/explore/' + kind)
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
            assert client.get('/api/explore/' + kind).json() == response.json()
        assert len(calls) == 2
        for query in ('?start=2001-02-29', '?start=2000-01-01&start=2001-01-01', '?size=100000', '?phrase=' + 'x' * 2100):
            assert client.get('/api/explore/overview' + query).status_code == 400
        assert client.get('/api/explore/unknown').status_code == 404
        assert client.post('/api/explore/overview', json={}).status_code == 405
        assert client.get('/api/explore/overview', headers={'Host': 'evil.invalid'}).status_code == 421
        assert client.get('/api/explore/overview', headers={'Origin': 'https://evil.invalid'}).status_code == 403
        assert len(calls) == 2
    with TestClient(create_app(make_archive(lambda r: httpx.Response(401, text='secret password')))) as client:
        response = client.get('/api/explore/overview')
        assert response.status_code == 503 and response.headers['retry-after'] == '10'
        assert 'secret' not in response.text and 'password' not in response.text


def test_vocabulary_bound_is_visible_and_equal_concurrent_reads_share_work():
    with patch('eqarchives_mcp.explore.MAX_VOCABULARY', 3):
        result = extract_phrases([hit()])
    assert result['vocabulary_limited'] and len(result['phrases']) <= 3
    calls = []
    async def es(request):
        calls.append(request)
        await asyncio.sleep(.01)
        return httpx.Response(200, json=overview())
    async def exercise():
        explorer = Explorer(make_archive(es))
        a, b = await asyncio.gather(explorer.get('overview', Selection()), explorer.get('overview', Selection()))
        assert a == b and len(calls) == 1
    asyncio.run(exercise())
