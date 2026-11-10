import asyncio
import hashlib
import json

import httpx
import pytest
from jsonschema import validate
from pydantic import ValidationError
from starlette.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from eqarchives_mcp.identity import (board_forums, discussion_identity, group_identity, literal,
                                    page_identity, scope_query, site_scope_url, VERSION)
from eqarchives_mcp.research import ResearchFilters, URLScope, filtered_query
from eqarchives_mcp.server import create_app
from test_archive import make_archive, run
from test_protocol import rpc
from test_research import never_embed, search_response


def website(path='forum.php?t=42', stamp='20000101000000', **fields):
    identifier = f'websites/example.org/{stamp}/{path}'
    return {'_id': identifier, '_source': {'id': identifier, 'title': 'Guild discussion',
        'url': f'https://web.archive.org/web/{stamp}/http://example.org/{path}',
        'capture_date': stamp[:4]+'-01-01T00:00:00Z', 'last_indexed': '2026-11-10T12:00:00Z', **fields}}


def proven(hit, topic='42'):
    parts = ['phpbb', 'http://example.org', '/', topic]
    hit['_source']['discussion'] = dict(zip(('platform', 'origin', 'forum', 'topic'), parts),
        version=VERSION, title='Guild discussion', key=hashlib.sha256(json.dumps(parts, separators=(',', ':')).encode()).hexdigest(),
        source_sha256='a'*64, evidence_sha256='b'*64, evidence_id=hit['_id'])
    return hit


def test_independent_dates_facets_collections_and_availability_are_closed_filters():
    filters = ResearchFilters(capture_from='1999-01-01', capture_to='2001-12-31',
        indexed_from='2026-11-01', indexed_to='2026-11-10', publication_from='1998-01-01',
        date_field='last_indexed', date_from='2026-11-05', mime_types=['text/html'],
        themes=['Guild'], tags=['cleric'], collections=['websites', 'newsgroups'],
        has_text=True, has_ocr=False, has_enrichment=True)
    clauses = filtered_query('', filters)['bool']['filter']
    assert {'range': {'last_indexed': {'gte': '2026-11-05T00:00:00.000Z', 'lte': '2026-11-10T23:59:59.999Z'}}} in clauses
    assert {'range': {'capture_date': {'gte': '1999-01-01T00:00:00.000Z', 'lte': '2001-12-31T23:59:59.999Z'}}} in clauses
    assert {'range': {'llm_guessed_date': {'gte': '1998-01-01T00:00:00.000Z'}}} in clauses
    assert clauses[:3] == [{'terms': {'mime_type': ['text/html']}}, {'terms': {'llm_content_flavour': ['Guild']}}, {'terms': {'llm_tags': ['cleric']}}]
    assert {'prefix': {'id': 'newsgroups/'}} in clauses[3]['bool']['should']
    assert clauses[-2] == {'bool': {'must_not': [{'bool': {'minimum_should_match': 1, 'should': [{'exists': {'field': 'llm_image_text_full'}}]}}]}}
    assert clauses[-1]['bool']['should'] == [{'exists': {'field': 'llm_model_name'}}, {'exists': {'field': 'llm_enrichment_signature'}}]


@pytest.mark.parametrize('args', [dict(indexed_from='2026-11-31'), dict(capture_from='2002-01-01', capture_to='2001-01-01'),
    dict(date_field='last_indexed', date_from='2026-12-01', indexed_to='2026-11-01'), dict(has_text=1),
    dict(collections=['_all']), dict(tags=['x']*11), dict(url_scopes=[{'url': 'file:///etc/passwd'}]),
    dict(url_scopes=[{'url': 'http://user:pass@example.org/'}]), dict(url_scopes=[{'url': 'http://example.org/#x'}]),
    dict(url_scopes=[{'url': 'http://example.org:999999/'}]), dict(url_scopes=[{'url': 'http://example.org/a/../b'}]),
    dict(url_scopes=[{'url': 'http://example.org/%00'}]), dict(url_scopes=[{'url': 'http://example.org/?q=1'}]),
    dict(url_scopes=[{'url': 'http://example.org/a', 'kind': 'host'}]),
    dict(url_scopes=[{'url': 'http://example.org/beqasylum', 'kind': 'ezboard_board'}])])
def test_bad_filters_fail_without_upstream(args):
    with pytest.raises(ValidationError):
        ResearchFilters(**args)


@pytest.mark.parametrize('kind,url,suffix', [
    ('directory', 'http://www.example.org/guild', r'guild(/.*)?'),
    ('directory', 'https://example.org/', '.*'),
    ('host', 'http://example.org/', '.*'),
    ('page', 'http://example.org:8080/a.php?a=1&b=@.*', r'a\.php\?a=1\&b=\@\.\*'),
    ('page', 'https://web.archive.org/web/20000101000000/http://example.org/x%20y', 'x y'),
])
def test_scopes_escape_every_lucene_operator_and_keep_account_boundary(kind, url, suffix):
    query = scope_query(URLScope(kind=kind, url=url))['regexp']['id']
    assert query['value'].endswith('/[0-9]{14}/'+suffix)
    assert query['max_determinized_states'] == 2000
    assert '(www\\.)?' in query['value']
    assert (':8080' in query['value']) == (':8080' in url)
    assert literal('@&#~<>') == r'\@\&\#\~\<\>'


def test_board_scope_only_adds_source_linked_forums_across_known_shards():
    scope = URLScope(kind='ezboard_board', url='http://pub4.ezboard.com/beqasylum')
    index = {'_id': 'websites/pub4.ezboard.com/20000101000000/beqasylum', '_source': {'text_full':
        '[General](http://pub8.ezboard.com/feqasylumgeneral) [Other](http://pub4.ezboard.com/fothergeneral) '
        '[Bad](http://attacker.example/feqasylumprivate)'}}
    assert board_forums(scope, [index]) == ['feqasylumgeneral']
    query = scope_query(scope, board_forums(scope, [index]))['regexp']['id']['value']
    assert '(server|pub|p|b)[0-9]+' in query and 'feqasylumgeneral' in query and 'fother' not in query
    async def exercise():
        calls = []
        def es(request):
            body = json.loads(request.content); calls.append(body)
            if body['size'] == 10:
                return search_response([index], 1)
            assert 'feqasylumgeneral' in json.dumps(body['query'])
            return search_response()
        archive = make_archive(es, never_embed)
        await archive.search_archive(filters=ResearchFilters(url_scopes=[scope]))
        assert calls[0]['_source'] == ['id', 'text_full'] and calls[1]['size'] == 20
    run(exercise())


def test_unverified_board_and_overlarge_membership_fail_explicitly():
    scope = URLScope(kind='ezboard_board', url='http://pub4.ezboard.com/beqasylum')
    archive = make_archive(lambda r: search_response(), never_embed)
    with pytest.raises(ToolError, match='No archived board-index'):
        run(archive.list_sites(filters=ResearchFilters(url_scopes=[scope])))
    hit = {'_id': 'websites/pub4.ezboard.com/20000101000000/beqasylum', '_source': {'text_full':
        ' '.join(f'http://pub4.ezboard.com/feqasylum{i}' for i in range(101))}}
    with pytest.raises(ValueError, match='100-forum'):
        board_forums(scope, [hit])
    with pytest.raises(ToolError, match='too broad'):
        run(make_archive(lambda r: search_response([hit], 1)).list_sites(filters=ResearchFilters(url_scopes=[scope])))
    assert board_forums(scope, [{'_id':'other', '_source':{'text_full':'http://pub4.ezboard.com/feqasylumgeneral'}}]) == []


def test_discussion_identity_requires_complete_proof_and_matching_origin_and_key():
    hit = proven(website())
    assert discussion_identity(hit).startswith('discussion:')
    assert group_identity(hit, 'page').startswith('page:')
    for field, bad in [('version','future'), ('key','c'*64), ('source_sha256','no'), ('evidence_id',''),
                       ('topic','0'), ('title',''), ('forum',''), ('origin','http://attacker.example'), ('platform','unknown')]:
        broken = json.loads(json.dumps(hit)); broken['_source']['discussion'][field] = bad
        assert discussion_identity(broken) is None, field
    assert discussion_identity(website()) is None
    assert page_identity(website(parent_id='attachment')) is None
    assert page_identity({'_id':'message/1','_source':{}}) is None
    broken = website(); broken['_source']['url'] = 'https://web.archive.org/web/20000101000000/http://user:pass@example.org/a'
    assert page_identity(broken) is None
    assert group_identity({'_id':'message/1'}, 'discussion') == 'record:message/1'


@pytest.mark.parametrize('group_by,expected', [('page', 3), ('discussion', 2)])
def test_grouping_preserves_pages_and_only_merges_verified_thread_members(group_by, expected):
    hits = [proven(website()), proven(website(stamp='20010101000000')),
            proven(website(path='forum.php?t=42&start=20')), website(path='unverified.html')]
    def es(request):
        body = json.loads(request.content)
        assert body['size'] == 1000 and 'text_full' not in body['_source'] and 'knn' not in body
        return search_response(hits, 4)
    page = run(make_archive(es, never_embed).search_archive(group_by=group_by, limit=1))
    assert page.total.value == expected and page.total.relation == 'eq'
    assert page.results[0].group.matching_captures_in_window == (2 if group_by == 'page' else 3)
    assert page.next_offset == 1 and page.scanned_documents == 4 and page.group_by == group_by
    last = run(make_archive(es).search_archive(group_by=group_by, offset=expected-1))
    assert last.next_offset is None and not last.has_more


def test_group_scan_bound_is_explicit_without_claiming_full_counts():
    page = run(make_archive(lambda r: search_response([website()]*1000, 1001, 'gte')).search_archive(group_by='page'))
    assert page.total.value == 1 and page.total.relation == 'gte'
    assert page.limit_reached and page.has_more and page.next_offset is None and 'Scan limit reached' in page.note


@pytest.mark.parametrize('discussion', [False, True])
def test_capture_history_returns_exact_dated_ids_and_revalidates_members(discussion):
    old = proven(website()); new = proven(website(stamp='20010101000000'))
    other = website(path='elsewhere.html')
    calls = []
    def es(request):
        body = json.loads(request.content); calls.append(body)
        if 'ids' in body['query']:
            return search_response([old], 1)
        if discussion:
            assert body['query']['bool']['filter'][0] == {'term': {'discussion.version': VERSION}}
        else:
            assert body['query']['bool']['should'][0]['regexp']['id']['value'].endswith(r'/[0-9]{14}/forum\.php\?t=42')
        return search_response([new, other, old], 3)
    page = run(make_archive(es, never_embed).list_captures(old['_id'], 'discussion' if discussion else 'page', limit=1))
    assert page.results[0].id == new['_id'] and page.total.value == 2 and page.next_offset == 1
    assert all('text_full' not in call['_source'] for call in calls)
    last = run(make_archive(es).list_captures(old['_id'], 'discussion' if discussion else 'page', offset=1))
    assert last.results[0].id == old['_id'] and last.next_offset is None


def test_capture_history_missing_unknown_and_scan_limit():
    with pytest.raises(ToolError, match='not found'):
        run(make_archive(lambda r: search_response()).list_captures('missing'))
    hit = {'_id':'mailing-lists/list/1', '_source':{'title':'message'}}
    page = run(make_archive(lambda r: search_response([hit], 1001, 'gte')).list_captures(hit['_id'], 'discussion'))
    assert page.results[0].id == hit['_id'] and page.group_by == 'page'
    assert page.limit_reached and page.total.relation == 'gte' and 'incomplete' in page.note


def site_metric(count=3, known=0):
    return {'doc_count':count, 'latest':{'value_as_string':'2026-11-10T12:00:00.000Z'},
        'first':{'value_as_string':'2026-11-09T10:00:00.000Z'}, 'known_first':{'value':known},
        'earliest':{'value_as_string':'1999-01-01T00:00:00.000Z'}, 'newest':{'value_as_string':'2001-01-01T00:00:00.000Z'}}


def sites_fixture(request):
    body = json.loads(request.content)
    assert body['_source'] is False and body['size'] == 0 and 'knn' not in body
    if 'sites' in body['aggs']:
        assert body['aggs']['sites']['terms']['order'] == [{'latest':'desc'}, {'_key':'asc'}]
        return search_response(aggregations={'sites':{'buckets':[{'key':'new.org','doc_count':1}, {'key':'legacy.org','doc_count':1}]}})
    assert body['aggs']['details']['filters']['filters']['new.org'] == {'term':{'domain_name':'new.org'}}
    return search_response(aggregations={'details':{'buckets':{'new.org':site_metric(10,10), 'legacy.org':site_metric(99,1)}}})


def test_recent_sites_exact_counts_from_second_query_and_historical_unknowns():
    page = run(make_archive(sites_fixture, never_embed).list_sites())
    assert [s.domain for s in page.sites] == ['new.org','legacy.org']
    assert page.sites[0].document_count == 10 and page.sites[0].first_indexed.startswith('2026-11-09')
    assert page.sites[1].document_count == 99 and page.sites[1].first_indexed is None
    assert page.sites[0].scope_url == 'http://new.org/' and not page.has_more
    assert 'not unique pages' in page.note
    assert run(make_archive(lambda r: search_response(aggregations={'sites':{'buckets':[]}})).list_sites()).sites == []
    assert run(make_archive(sites_fixture).list_sites(limit=1)).has_more


def test_fixed_collection_directory_and_facets_use_readonly_bounded_aggregations():
    def es(request):
        body = json.loads(request.content)
        assert set(body['aggs']['collections']['filters']['filters']) == {'websites','mailing_lists','newsgroups'}
        return search_response(aggregations={'collections':{'buckets':{
            'websites':{'doc_count':5},'mailing_lists':{'doc_count':2},'newsgroups':{'doc_count':0}}}})
    archive = make_archive(es, never_embed)
    page = run(archive.list_sources('collection', limit=1))
    assert page.sources[0].value == 'mailing_lists' and page.next_after == 'mailing_lists'
    last = run(archive.list_sources('collection', after=page.next_after, limit=1))
    assert last.sources[0].value == 'websites' and last.next_after is None


@pytest.mark.parametrize('kind,field', [('theme','llm_content_flavour'),('tag','llm_tags'),('mime_type','mime_type')])
def test_added_facets_use_fixed_keyword_fields(kind,field):
    def es(request):
        body = json.loads(request.content)
        assert body['aggs']['sources']['composite']['sources'][0]['value']['terms']['field'] == field
        return search_response(aggregations={'sources':{'buckets':[]}})
    assert run(make_archive(es).list_sources(kind)).sources == []


@pytest.mark.parametrize('method,args', [('list_sites',{}),('list_captures',{'document_id':'x'})])
def test_new_tools_share_admission(method,args):
    archive = make_archive(lambda r: pytest.fail('busy request reached ES'))
    archive.requests = asyncio.Semaphore(0)
    with pytest.raises(ToolError,match='busy'):
        run(getattr(archive,method)(**args))


@pytest.mark.parametrize('method,args', [('list_sites',{}),('search_archive',{'group_by':'page'}),
    ('list_sources',{'source_type':'collection'}),('list_captures',{'document_id':'x'})])
def test_malformed_new_results_have_sanitized_errors(method,args):
    calls = []
    def es(request):
        calls.append(1)
        if method == 'list_captures' and len(calls) == 1:
            return search_response([website()],1)
        return httpx.Response(200,json={'hits':{'hits':[], 'total':'secret'}})
    with pytest.raises(ToolError,match='temporarily') as error:
        run(getattr(make_archive(es),method)(**args))
    assert 'secret' not in str(error.value)


def test_new_tools_roundtrip_schemas_and_date_metadata_through_mcp():
    def es(request):
        body = json.loads(request.content)
        if 'aggs' in body:
            return sites_fixture(request)
        return search_response([website(llm_tags=['cleric','guild'], llm_content_flavour='Guild',text_full='source')],1)
    with TestClient(create_app(make_archive(es,never_embed))) as client:
        tools = {t['name']:t for t in rpc(client,'tools/list').json()['result']['tools']}
        for name,args in [('list_sites',{}),('list_captures',{'id':website()['_id']}),
                          ('search_archive',{'sort':'last_indexed_desc','filters':{'date_field':'last_indexed','date_from':'2026-11-01'}})]:
            response = rpc(client,'tools/call',{'name':name,'arguments':args}).json()['result']
            assert not response.get('isError'), response
            value = response['structuredContent']
            validate(value,tools[name]['outputSchema'])
            assert json.loads(response['content'][0]['text']) == value
            if name == 'search_archive':
                assert value['results'][0]['metadata']['last_indexed'] == '2026-11-10T12:00:00Z'
                assert json.loads(value['results'][0]['metadata']['llm_tags']) == ['cleric','guild']


@pytest.mark.parametrize('name,args', [
    ('list_sites', {'limit':51}), ('list_sites', {'limit':True}), ('list_sites', {'filters':{'query':{'match_all':{}}}}),
    ('list_captures', {'id':'x','group_by':'_script'}), ('list_captures', {'id':'x','offset':1000}),
    ('list_captures', {'id':'x','limit':51}), ('list_captures', {'id':['x']}),
    ('search_archive', {'group_by':'title'}), ('search_archive', {'filters':{'has_ocr':'true'}}),
    ('search_archive', {'filters':{'url_scopes':[{'kind':'host','url':'http://.*.example.org/'}]}}),
])
def test_new_invalid_protocol_arguments_never_reach_upstream(name,args):
    archive = make_archive(lambda r: pytest.fail('Invalid query reached upstream'), never_embed)
    with TestClient(create_app(archive)) as client:
        result = rpc(client,'tools/call',{'name':name,'arguments':args}).json()['result']
        assert result['isError']


def test_history_legacy_url_fallback_shares_the_analyzed_text_bound():
    for path, expected in [('small.html',3), ('a/'*101,2)]:
        hit = website(path=path)
        def es(request):
            body = json.loads(request.content)
            if 'ids' not in body['query']:
                branches = body['query']['bool']['should']
                assert len(branches) == expected
                assert branches[1] == {'ids':{'values':[hit['_id']]}}
            return search_response([hit],1)
        assert run(make_archive(es).list_captures(hit['_id'])).results[0].id == hit['_id']


def test_borrowed_discussion_proof_cannot_group_an_unrelated_stored_page():
    hit = proven(website())
    hit['_id'] = hit['_source']['id'] = hit['_id'].replace('example.org','other.org')
    assert discussion_identity(hit) is None
    scope = URLScope(kind='ezboard_board',url='http://pub4.ezboard.com/beqasylum')
    assert board_forums(scope,[{'_id':'websites/pub4.ezboard.com/20000101000000/beqasylum','_source':{'text_full':None}}]) == []


@pytest.mark.parametrize('domain,expected', [('www.example.org_80','http://www.example.org:80/'),
    ('example.org_443','http://example.org:443/'),('example.org:8080','http://example.org:8080/'),
    ('user:pass@example.org',None),('example.org/path',None),('bad host',None),(None,None)])
def test_site_scope_links_are_safe_and_legacy_default_ports_are_usable(domain,expected):
    assert site_scope_url(domain) == expected



def test_null_legacy_url_uses_stored_page_identity_and_encoded_controls_are_rejected():
    hit = website(url=None)
    assert page_identity(hit)[0] == 'page:http://example.org/forum.php?t=42'
    with pytest.raises(ValidationError):
        URLScope(url='http://example.org/a?x=%00',kind='page')


@pytest.mark.parametrize('board_url', ['http://pub110.ezboard.com/beqasylum',
    'http://pub110.ezboard.com/beqasylum/', 'http://pub110.ezboard.com/beqasylum/index.html'])
def test_real_archive_board_directory_indices_establish_forum_membership(board_url):
    import re
    scope = URLScope(url=board_url,kind='ezboard_board')
    hit = {'_id':'websites/pub110.ezboard.com/20020805200450/beqasylum/index.html',
           '_source':{'text_full':'[General](http://pub110.ezboard.com/feqasylumgeneral)'}}
    pattern = scope_query(scope)['regexp']['id']['value']
    assert re.fullmatch(pattern,hit['_id'])
    assert re.fullmatch(pattern,hit['_id'].removesuffix('/index.html'))
    assert not re.fullmatch(pattern,hit['_id'].replace('/index.html','.showLocalUserPublicProfile?login=someone'))
    assert board_forums(scope,[hit]) == ['feqasylumgeneral']
