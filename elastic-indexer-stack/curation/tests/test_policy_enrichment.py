from contextlib import closing
from copy import deepcopy
from functools import partial
import json

import pytest

from daily_budget import DailyBudget, EnrichmentPriorityPause
from indexer import passages, search_enrichment
from indexer.capture_enrichment import Enricher
from policy_enrichment import FRONTIER_PAGE_SIZE, capture_identity, eligible, prepare_refresh, refresh, run_enrichment
from policy_maintenance import MaintenanceServices, State, identity, repair, run
from policy_pool import PreparationPool
from policy_preparation import prepare, rebuild_view
from policy_sources import ServiceError, SourceError
from test_enrichment import RESULT, response
from test_policy_maintenance import Services, V, fixture


class Luna:
    def __init__(self, *_):
        self.calls = []

    def request(self, payload):
        self.calls.append(json.loads(payload['input']))
        result = {**RESULT, 'llm_summary': 'Guild defeats Nagafen.', 'content_role': 'article',
                  'topics': [{'name': 'defeated Nagafen', 'excerpt': 'Our guild defeated Nagafen.'}],
                  'entities': [{'kind': 'npc', 'name': 'Nagafen', 'excerpt': 'Our guild defeated Nagafen.'}]}
        return response(result)

    def close(self):
        pass


class Maintenance(Services):
    def select_enrichment(self, cursor):
        return [key for key, hit in sorted(self.hits.items()) if key > cursor and eligible(hit['_source'])][:100]


def budget(tmp_path, daily=2):
    account = DailyBudget(tmp_path / 'luna')
    account.configure({'enabled': False, 'daily_usd': daily, 'monthly_usd': 10, 'min_grade': 2,
                       'grading_criteria': '', 'revision': 0})
    return account


def test_refresh_replaces_summary_and_vector_atomically_from_verified_passages(tmp_path):
    hit, sources = fixture(tmp_path)
    service = Maintenance([hit])
    repair(service, sources, hit['_id'], lambda: None)
    indexed = service.hits[hit['_id']]['_source']
    indexed.update(llm_guessed_date='2000-01-01', llm_extracted_dates=[{'date': '2000-01-01'}])
    before = deepcopy(indexed)
    luna = Luna()
    enricher = Enricher(tmp_path / 'enrichment', 'unused', client_factory=lambda _: luna)
    try:
        account = budget(tmp_path)
        with account.bind(purpose='maintenance'):
            assert refresh(service, sources, hit['_id'], capture_identity(hit['_id'])[0], enricher, lambda: None) == 'updated'
        patch = service.updates[-1]
        assert patch['llm_summary'] == 'Guild defeats Nagafen.' and patch['llm_summary_vector'] == V
        assert patch['llm_topics'] == ['defeated nagafen'] and patch['llm_entities'] == ['npc: nagafen']
        assert patch['llm_enrichment_passage_version'] == passages.VERSION
        assert patch['llm_search_enrichment_version'] == search_enrichment.VERSION
        assert 'enrichment_view_sha256' not in luna.calls[0]
        assert 'Page URL' not in luna.calls[0]['complete_source_markdown']
        assert 'Nagafen' in str(luna.calls[0]['search_passages'])
        assert account.snapshot()['estimated_usd'] > 0
        for field in ('llm_guessed_date', 'llm_extracted_dates', 'capture_date', 'last_indexed', 'discussion', 'text_full', 'text'):
            assert indexed[field] == before[field] and field not in patch
        assert refresh(service, sources, hit['_id'], '', enricher, lambda: None) == 'skipped'
        assert len(luna.calls) == 1
    finally:
        enricher.close()
        sources.close()


@pytest.mark.parametrize('identifier', [
    'websites/guild.example/20000101000000/article.html',
    'newsgroups/alt.games.everquest-msg-1.txt',
    'mailing-lists/guild/1.txt',
])
def test_verified_checkpoint_keeps_exact_grounding_and_legacy_source(tmp_path, monkeypatch, identifier):
    prose = 'Our guild defeated Nagafen and returned to the Temple of Veeshan. ' * 160
    hit, sources = fixture(tmp_path, raw=f'<html><title>Guild news</title><body><p>{prose}</p></body></html>',
                           identifier=identifier)
    if not identifier.startswith('websites/'):
        reader = '# Subject: Guild news\n# From: An archive author\n\n---\n\n' + prose
        plan = passages.prepare(reader, identifier, mime_type='text/plain')
        saved = hit['_source']
        saved.update(text_full=reader, mime_type='text/plain', text=[],
                     text_passage_provenance={'version': 'eq-passages-480-v10',
                         'sources': [{**plan.provenance(), 'version': 'eq-passages-480-v10'}]})
    try:
        service = Maintenance([hit])
        repair(service, sources, identifier, lambda: None)
        before = prepare(service.get(identifier), sources, enrichment=True)
        assert len(before.plan.passages) > 1

        def rebuild(*args, **kwargs):
            raise AssertionError('a verified checkpoint rebuilt body passages')

        monkeypatch.setattr(passages, 'prepare', rebuild)
        current = prepare_refresh(service, sources, identifier, capture_identity(identifier)[0], lambda: None)
        assert current.checkpoint_view
        assert (current.source, current.capture['sha256']) == before.enrichment_source
        assert current.view == before.plan.view
        assert current.grounding == search_enrichment.evidence_parts(before.plan.view)
        with closing(PreparationPool(sources.roots, sources.publication, enrichment=True)) as pool:
            task = pool.submit((service.get(identifier), current.capture['url']))
            spawned = task.result(lambda: None)
            assert spawned.hit == current.hit and spawned.capture == current.capture
            assert spawned.source == current.source and spawned.grounding == current.grounding
            assert spawned.checkpoint_view
    finally:
        sources.close()


@pytest.mark.parametrize('damage', ['gap', 'passage_text', 'decoded_html', 'view_digest', 'reader', 'raw'])
def test_enrichment_requires_checkpoint_proof_or_verified_rebuild(tmp_path, damage):
    hit, sources = fixture(tmp_path, raw='<html><title>Guild news</title><body>Defeated Nagafen.</body></html>')
    try:
        service = Maintenance([hit])
        identifier = hit['_id']
        repair(service, sources, identifier, lambda: None)
        saved = service.hits[identifier]['_source']
        baseline = prepare(service.get(identifier), sources, enrichment=True)
        binding = saved['text_passage_provenance']['sources'][0]
        if damage == 'gap':
            saved['text'][0]['source_start'] += 1
        elif damage == 'passage_text':
            saved['text'][0]['text_chunk'] = 'x' * len(saved['text'][0]['text_chunk'])
        elif damage == 'decoded_html':
            binding['html_sha256'] = '0' * 64
        elif damage == 'view_digest':
            binding['view_sha256'] = '0' * 64
        elif damage == 'reader':
            saved['text_full'] += 'unexpected copy'
        elif damage == 'raw':
            (sources.roots[0] / identifier).write_text('changed archive file')

        def refresh():
            return prepare_refresh(service, sources, identifier, capture_identity(identifier)[0], lambda: None)

        reason = {'view_digest': 'checkpoint_integrity', 'reader': 'reader_integrity', 'raw': 'source_integrity'}.get(damage)
        if reason:
            with pytest.raises(SourceError, match=reason):
                refresh()
        else:
            restored = refresh()
            assert restored.checkpoint_view is False
            assert restored.view == baseline.plan.view
            assert (restored.source, restored.capture['sha256']) == baseline.enrichment_source
    finally:
        sources.close()


def test_checkpoint_checks_overlapping_bytes_even_when_new_suffix_is_correct():
    first = {'source_start': 0, 'source_end': 5, 'text_chunk': 'hello'}
    next_passage = {'source_start': 3, 'source_end': 8, 'text_chunk': 'lo!!!'}
    assert rebuild_view([first, next_passage]) == 'hello!!!'
    assert rebuild_view([first, {**next_passage, 'text_chunk': 'xx!!!'}]) is None


def test_paid_budget_wait_does_not_hold_the_body_cursor_or_spend(tmp_path):
    ready, sources = fixture(tmp_path, identifier='websites/guild.example/20000101000000/a.html')
    stale, _ = fixture(tmp_path, identifier='websites/guild.example/20000101000000/b.html')
    service = Maintenance([ready, stale])
    repair(service, sources, ready['_id'], lambda: None)
    account = budget(tmp_path, daily=0.001)
    state = State(tmp_path / 'state')
    try:
        outcome = run(tmp_path, state, service, sources, 'eqarchives-policy-'+identity()+'-1',
                      should_yield=lambda: False, budget_root=account.root)
        paid = outcome['enrichment']
        assert paid['phase'] == 'waiting_budget' and paid['updated'] == paid['failed'] == 0
        assert state.enrichment_progress(search_enrichment.VERSION)['cursor'] == ''
        assert service.hits[stale['_id']]['_source']['text_chunking_version'] == passages.VERSION
        assert account.snapshot()['estimated_usd'] == account.snapshot()['unresolved_usd'] == 0
    finally:
        state.close()
        sources.close()


def test_retry_reuses_paid_response_after_embedding_outage_and_moves_to_later_records(tmp_path):
    a, sources = fixture(tmp_path, identifier='websites/guild.example/20000101000000/a.html')
    b, _ = fixture(tmp_path, identifier='websites/other.example/20000101000000/b.html')
    service = Maintenance([a, b])
    repair(service, sources, a['_id'], lambda: None)
    repair(service, sources, b['_id'], lambda: None)
    initial = len(service.updates)
    account = budget(tmp_path)
    luna = Luna()
    factory = lambda *a,client_factory,**kw: Enricher(*a,client_factory=lambda _: luna,**kw)
    state = State(tmp_path / 'state')
    vectors = service.vectors
    try:
        def outage(*_):
            raise ServiceError('embedding_unavailable')
        service.vectors = outage
        run_slice = partial(run_enrichment,state,service,sources,'policy-fixture',account.root,lambda:None,
                            enricher_factory=factory)
        result = run_slice()
        assert result['phase']=='retry_wait' and result['cursor']==''
        assert len(luna.calls)==1 and len(service.updates)==initial
        state.enrichment_progress(search_enrichment.VERSION, phase='bounded')
        service.vectors = vectors
        result = run_slice()
        assert result['updated']==2 and result['cursor']==b['_id']
        assert len(luna.calls)==2 and len(service.updates)==initial+2
    finally:
        state.close()
        sources.close()


def test_same_grounding_after_policy_change_only_advances_marker(tmp_path):
    hit, sources = fixture(tmp_path)
    service = Maintenance([hit])
    repair(service, sources, hit['_id'], lambda: None)
    account = budget(tmp_path)
    luna = Luna()
    enricher = Enricher(tmp_path / 'enrichment', 'unused', client_factory=lambda _: luna)
    try:
        url = capture_identity(hit['_id'])[0]
        with account.bind(purpose='maintenance'):
            assert refresh(service, sources, hit['_id'], url, enricher, lambda: None) == 'updated'
        saved = service.hits[hit['_id']]['_source']
        saved['llm_enrichment_passage_version'] = 'eq-passages-480-v12'
        assert refresh(service, sources, hit['_id'], url, enricher, lambda: None) == 'reused'
        assert service.updates[-1] == {'llm_enrichment_passage_version': passages.VERSION}
        assert len(luna.calls) == 1
        saved.pop('llm_enrichment_passage_version')
        assert not eligible(saved)  # Original v13 imports are adopted.
    finally:
        enricher.close()
        sources.close()


def test_bad_source_and_conflicting_write_advance_to_next_page(tmp_path):
    a, sources = fixture(tmp_path, identifier='websites/guild.example/20000101000000/a.html')
    b, _ = fixture(tmp_path, identifier='websites/guild.example/20000101000000/b.html')
    c, _ = fixture(tmp_path, identifier='websites/guild.example/20000101000000/c.html')
    service = Maintenance([a, b, c])
    for hit in (a, b, c):
        repair(service,sources,hit['_id'],lambda:None)
    (sources.roots[0] / a['_id']).write_text('unexpected replacement')
    save = service.update
    def update(hit, patch):
        if hit['_id']==b['_id']:
            raise SourceError('concurrent_update')
        save(hit,patch)
    service.update = update
    account = budget(tmp_path)
    luna = Luna()
    state = State(tmp_path / 'state')
    try:
        outcome=run_enrichment(state,service,sources,'policy-fixture',account.root,lambda:None,
            enricher_factory=lambda *a,client_factory,**kw: Enricher(*a,client_factory=lambda _:luna,**kw))
        assert outcome['updated']==1 and outcome['failed']==2 and outcome['cursor']==c['_id']
        assert outcome['last_reason']=='concurrent_update'
        # Identical source bytes on distinct URLs retain distinct paid requests.
        assert len(luna.calls)==2
    finally:
        state.close()
        sources.close()


def test_alias_scope_and_priority_put_active_imports_first(tmp_path):
    assert capture_identity('websites/www.guild.example:80/20000101000000/a')[1] == capture_identity(
        'websites/guild.example/20050101000000/b')[1]
    account = budget(tmp_path)
    with account.bind(purpose='enrichment', owner='import-job'):
        with account.bind(purpose='maintenance', owner='maintenance-job'), pytest.raises(EnrichmentPriorityPause):
            account.reserve(0.05)
    with account.bind(purpose='maintenance', owner='maintenance-job'):
        account.reserve(0.05)
        with account.bind(), pytest.raises(EnrichmentPriorityPause):
            account.reserve(0.05)
    with account.bind():
        account.reserve(0.05)


def test_paid_selection_is_bounded_and_excludes_current_enrichment():
    services = MaintenanceServices.__new__(MaintenanceServices)
    services.index = 'eq-archive-0'
    cursor = 'newsgroups/alt.games.everquest-msg-10.txt'
    next_id = 'newsgroups/alt.games.everquest-msg-11.txt'
    def request(method, path, body, **kwargs):
        assert method == 'POST' and path == '/eq-archive-0/_search'
        assert body['size'] == 100 and body['_source'] is False
        clauses = body['query']['bool']
        assert {'range': {'id': {'gt': cursor}}} in clauses['filter']
        assert {'nested': {'path': 'text', 'query': {'exists': {'field': 'text.text_chunk'}},
                           'score_mode': 'none'}} in clauses['filter']
        assert {'term': {'llm_search_enrichment_version': search_enrichment.VERSION}} in clauses['must_not'][0]['bool']['filter']
        return {'hits': {'hits': [{'_id': next_id, '_index': services.index}]}}
    services.request = request
    assert services.select_enrichment(cursor) == [next_id]


@pytest.mark.parametrize('collection', ['websites', 'mailing-lists', 'newsgroups'])
def test_frontier_pages_use_bounded_id_reads_with_the_current_policy(collection):
    services = MaintenanceServices.__new__(MaintenanceServices)
    services.index = 'eq-archive-0'
    cursor = f'{collection}/0000'
    expected = [f'{collection}/{number:04d}' for number in range(1, FRONTIER_PAGE_SIZE + 1)]

    def request(method, path, body, *, limit):
        assert method == 'POST' and path == '/eq-archive-0/_search?allow_partial_search_results=false'
        assert body['size'] == FRONTIER_PAGE_SIZE and body['_source'] is False
        assert body['track_total_hits'] is False and body['sort'] == [{'id': 'asc'}]
        assert limit == 4 * 1024 * 1024
        filters = body['query']['bool']['filter']
        assert {'prefix': {'id': collection+'/'}} in filters
        assert {'range': {'id': {'gt': cursor}}} in filters
        assert {'nested': {'path':'text', 'query':{'exists':{'field':'text.text_chunk'}}, 'score_mode':'none'}} in filters
        policy = services.current_policy() if collection == 'websites' else {'term': {'text_chunking_version': passages.VERSION}}
        assert policy in filters
        return {'timed_out': False, '_shards': {'failed': 0},
                'hits': {'hits': [{'_id': identifier, '_index': services.index} for identifier in expected]}}

    services.request = request
    assert services.select_enrichment(cursor, collection=collection, limit=FRONTIER_PAGE_SIZE) == expected
    with pytest.raises(ValueError, match='bound'):
        services.select_enrichment(cursor, collection=collection, limit=FRONTIER_PAGE_SIZE + 1)


@pytest.mark.parametrize('changes,hits', [
    ({'timed_out': True}, []),
    ({'_shards': {'failed': 1}}, []),
    ({'_shards': {}}, []),
    ({}, [{'_id':'websites/other', '_index':'eq-archive-0'}]),
    ({}, [{'_id':'newsgroups/0000', '_index':'eq-archive-0'}]),
])
def test_frontier_rejects_incomplete_or_wrong_collection_pages(changes, hits):
    services = MaintenanceServices.__new__(MaintenanceServices)
    services.index = 'eq-archive-0'
    services.request = lambda *args, **kwargs: {
        'timed_out': False, '_shards': {'failed': 0}, 'hits': {'hits': hits}, **changes,
    }
    with pytest.raises(ServiceError):
        services.select_enrichment('newsgroups/0000', collection='newsgroups')
