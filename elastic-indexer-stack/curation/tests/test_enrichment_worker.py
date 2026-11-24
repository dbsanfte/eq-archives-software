import json
from itertools import cycle
from threading import Barrier, Lock

import pytest

from common import digest
from daily_budget import DailyBudget
from enrichment_worker import State, run
from indexer import passages, search_enrichment
from indexer.capture_enrichment import Enricher
from policy_maintenance import State as PolicyState, identity, repair
from test_enrichment import RESULT, response
from test_policy_enrichment import Maintenance, budget
from test_policy_maintenance import V, fixture


class Service(Maintenance):
    def __init__(self, hits):
        super().__init__(hits)
        self.waves = []

    def select_enrichment(self, cursor, *, collection, limit):
        return [key for key in super().select_enrichment(cursor) if key.startswith(collection + '/')][:limit]

    def vectors(self, inputs, check):
        self.waves.append(list(inputs))
        return super().vectors(inputs, check)


def prepared_service(root):
    identifiers = [
        'websites/guild.example/20000101000000/page-one.html',
        'mailing-lists/Clerics/2.json',
        'newsgroups/alt.games.everquest-msg-3.txt',
        'websites/guild.example/20000101000000/page-four.html',
    ]
    hits = []
    for index, identifier in enumerate(identifiers, 1):
        raw = f'<title>Guild news</title><p>Our guild defeated Nagafen on expedition number {index}.</p>'
        hit, sources = fixture(root, raw=raw, identifier=identifier)
        sources.close()
        if not identifier.startswith('websites/'):
            source = hit['_source']
            source['mime_type'] = 'text/plain'
            source['text_full'] = f'# Subject: Guild news\n\nOur guild defeated Nagafen on expedition number {index}.'
            plan = passages.prepare(source['text_full'], identifier, source['title'], mime_type='text/plain')
            source['text_passage_provenance'] = {'version': 'eq-passages-480-v10', 'sources': [
                {**plan.provenance(), 'version': 'eq-passages-480-v10'}]}
        hits.append(hit)
    from policy_sources import Sources
    sources = Sources([root / 'archive'])
    service = Service(hits)
    for hit in hits:
        repair(service, sources, hit['_id'], lambda: None)
    service.updates.clear()
    service.waves.clear()
    return service, sources


def factory(calls, barrier=None):
    lock = Lock()

    class Luna:
        def __init__(self, *_):
            pass

        def request(self, payload):
            supplied = json.loads(payload['input'])
            assert set(supplied) == {'search_passages'}
            assert not {'date_evidence', 'llm_extracted_dates', 'llm_guessed_date'} & set(
                payload['text']['format']['schema']['properties'])
            with lock:
                calls.append(supplied)
            if barrier:
                barrier.wait(timeout=8)
            passage = ' '.join(supplied['search_passages'])
            index = next(i for i in range(1, 5) if f'number {i}.' in passage)
            return response({**{key: RESULT[key] for key in ('llm_tags','llm_content_flavour')},
                             'llm_summary': f'Guild expedition number {index}.', 'content_role': 'discussion',
                             'topics': [], 'entities': [{'kind': 'npc', 'name': 'Nagafen', 'excerpt': 'defeated Nagafen'}]})

        def close(self):
            pass

    return lambda *args,client_factory,**kwargs: Enricher(*args,client_factory=Luna,**kwargs)


def test_four_paid_requests_overlap_and_share_one_embedding_wave(tmp_path):
    service, sources = prepared_service(tmp_path)
    account = budget(tmp_path)
    state = State(tmp_path / 'state',identity())
    calls = []
    try:
        result = run(tmp_path,state,service,sources,'fixture',account,maximum=4,should_yield=lambda:False,
                     enricher_factory=factory(calls,Barrier(4)))
        assert result['enrichment']['updated'] == 4
        assert len(calls) == 4 and len(service.waves) == 1 and len(service.waves[0]) == 4
        assert account.snapshot()['estimated_usd'] > 0
        assert not account.snapshot()['enrichment_pending']
        assert len(service.updates) == 4 and all(p['llm_summary_vector'] == V for p in service.updates)
        assert all(p['llm_entities'] == ['npc: nagafen'] and 'llm_guessed_date' not in p for p in service.updates)
        assert not state.ready(cycle(('websites','mailing-lists','newsgroups')), 4)
    finally:
        state.close()
        sources.close()


def test_budget_pause_preserves_queue_and_embedding_failure_reuses_paid_wave(tmp_path):
    service, sources = prepared_service(tmp_path)
    account = budget(tmp_path,daily=0.001)
    state = State(tmp_path / 'state',identity())
    calls = []
    try:
        def attempt():
            return run(tmp_path,state,service,sources,'fixture',account,maximum=4,should_yield=lambda:False,
                       enricher_factory=factory(calls))
        first = attempt()
        assert first['phase'] == 'waiting_budget' and first['enrichment']['updated'] == 0
        assert not calls and not service.updates and account.snapshot()['unresolved_usd'] == 0
        account.configure(dict(enabled=False,daily_usd=2,monthly_usd=10,min_grade=2,grading_criteria='',revision=1))
        vectors = service.vectors
        service.vectors = lambda inputs,check: [V]
        retry = attempt()
        assert retry['phase'] == 'retry_wait' and not service.updates and len(calls) == 4
        service.vectors = vectors
        assert attempt()['enrichment']['updated'] == 4 and len(calls) == 4
    finally:
        state.close()
        sources.close()


def test_migrates_paid_totals_and_only_matching_frontier(tmp_path):
    state = PolicyState(tmp_path)
    try:
        old_id = 'mailing-lists/Clerics/43.json'
        state.enrichment_progress(search_enrichment.VERSION, cursor=old_id, result='updated')
    finally:
        state.close()
    dedicated = State(tmp_path,identity())
    try:
        assert dedicated.counts()['updated'] == 1
        frontiers = dict(dedicated.db.connection().execute('SELECT collection,cursor FROM frontiers'))
        assert frontiers == {'mailing-lists':old_id, 'newsgroups':'', 'websites':''}
    finally:
        dedicated.close()


def test_maintenance_reuses_legacy_paid_result_without_submitting_again(tmp_path):
    account = budget(tmp_path)
    grounding=('EverQuest guild raid report.',)
    capture={'url':'http://guild.example/news','archive_path':'websites/guild.example/20000101000000/news',
             'sha256':digest(b'old-source')}
    calls=[]
    class OldLuna:
        def request(self,payload):
            calls.append(payload)
            return response({**RESULT,'content_role':'article','topics':[],'entities':[]})
        def close(self):
            pass
    with account.bind(purpose='maintenance'):
        client=Enricher(tmp_path/'paid','unused',client_factory=lambda _: OldLuna())
        try:
            legacy=client.enrich(capture,'EverQuest guild raid report.',grounding[0])
            assert client.enrich_for_maintenance(capture,'EverQuest guild raid report.',grounding)==legacy
            assert len(calls)==1
        finally:
            client.close()
