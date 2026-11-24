from copy import deepcopy
from functools import partial
import json

import pytest

from daily_budget import DailyBudget, EnrichmentPriorityPause
from indexer import passages, search_enrichment
from indexer.capture_enrichment import Enricher
from policy_enrichment import capture_identity, eligible, refresh, run_enrichment
from policy_maintenance import MaintenanceServices, State, identity, repair, run
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
