import json
from contextlib import closing
from itertools import cycle
from threading import Barrier, Event, Lock

import pytest

from common import digest
from daily_budget import DailyBudget
import enrichment_worker
from enrichment_worker import MAX_WORKERS, WORKERS, State, run
from indexer import passages, search_enrichment
from indexer.capture_enrichment import Enricher
from policy_maintenance import State as PolicyState, identity, repair
from policy_scheduler import receipt
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


def prepared_service(root, count=4):
    identifiers = [
        'websites/guild.example/20000101000000/page-one.html',
        'mailing-lists/Clerics/2.json',
        'newsgroups/alt.games.everquest-msg-3.txt',
        'websites/guild.example/20000101000000/page-four.html',
    ]
    identifiers += [f'websites/guild.example/20000101000000/page-{index}.html'
                    for index in range(5, count + 1)]
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


def factory(calls, barrier=None, on_request=None):
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
            index = next(i for i in range(1, MAX_WORKERS + 1) if f'number {i}.' in passage)
            if on_request:
                on_request(index)
            return response({**{key: RESULT[key] for key in ('llm_tags','llm_content_flavour')},
                             'llm_summary': f'Guild expedition number {index}.', 'content_role': 'discussion',
                             'topics': [], 'entities': [{'kind': 'npc', 'name': 'Nagafen', 'excerpt': 'defeated Nagafen'}]})

        def close(self):
            pass

    return lambda *args,client_factory,**kwargs: Enricher(*args,client_factory=Luna,**kwargs)


@pytest.mark.parametrize('worker_count', [WORKERS, MAX_WORKERS])
def test_paid_requests_overlap_at_default_and_maximum_concurrency(tmp_path, worker_count, monkeypatch):
    service, sources = prepared_service(tmp_path, count=worker_count)
    account = budget(tmp_path)
    state = State(tmp_path / 'indexing-state', identity())
    calls = []
    initialized = []
    install = enrichment_worker.install_scope_totals

    def record_initialization(db):
        initialized.append(db.execute('PRAGMA database_list').fetchone()[2])
        install(db)

    monkeypatch.setattr(enrichment_worker, 'install_scope_totals', record_initialization)
    try:
        job = 'eqarchives-enrichment-' + identity() + '-1'
        result = run(tmp_path,state,service,sources,job,account,maximum=worker_count,should_yield=lambda:False,
                     workers=worker_count, enricher_factory=factory(calls, Barrier(worker_count)))
        scheduler_state = {'name':job, **{key:result[key] for key in ('image','policy','key')}}
        assert receipt(tmp_path, scheduler_state)['execution'] == result['execution']
        assert result['execution']['checkpoint_views'] == worker_count
        assert result['execution']['rebuilt_views'] == 0
        assert result['enrichment']['updated'] == worker_count
        assert len(calls) == worker_count and sum(map(len, service.waves)) == worker_count
        assert len(initialized) == len(set(initialized)) == 3
        assert account.snapshot()['estimated_usd'] > 0
        assert not account.snapshot()['enrichment_pending']
        assert len(service.updates) == worker_count and all(p['llm_summary_vector'] == V for p in service.updates)
        assert all(p['llm_entities'] == ['npc: nagafen'] and 'llm_guessed_date' not in p for p in service.updates)
        assert not state.ready(cycle(('websites','mailing-lists','newsgroups')), 4)
    finally:
        state.close()
        sources.close()


def test_finished_slot_starts_more_work_with_one_request_blocked(tmp_path):
    service, sources = prepared_service(tmp_path, count=6)
    state = State(tmp_path / 'state', identity())
    account = budget(tmp_path)
    slow_started, refilled, later_written = Event(), Event(), Event()
    update = service.update

    def on_request(number):
        if number == 1:
            slow_started.set()
            assert later_written.wait(5), 'later records waited for the slow request'
        elif number == 3:
            assert slow_started.wait(3)
            refilled.set()

    def record_write(hit, patch):
        update(hit, patch)
        if hit['_id'].startswith('newsgroups/'):
            assert refilled.is_set()
            later_written.set()

    service.update = record_write
    try:
        result = run(tmp_path, state, service, sources, 'fixture', account, maximum=6,
                     workers=2, should_yield=lambda:False, enricher_factory=factory([], on_request=on_request))
        assert refilled.is_set()
        assert result['updated'] == 6 and result['failed'] == 0
        assert len(service.updates) == 6
    finally:
        state.close()
        sources.close()


def test_over_limit_worker_count_is_rejected_before_work(tmp_path):
    with pytest.raises(ValueError, match='Invalid historical enrichment slice bounds'):
        run(tmp_path, None, None, None, 'fixture', None, workers=MAX_WORKERS + 1)


def test_durable_queue_refills_before_previous_page_has_finished(tmp_path):
    size = enrichment_worker.PAGE_SIZE
    identifiers = [f'websites/guild.example/20000101000000/page-{number:03d}'
                   for number in range(size + size // 2)]

    class Frontier:
        def select_enrichment(self, cursor, *, collection, limit):
            return [key for key in identifiers if key.startswith(collection + '/') and key > cursor][:limit]

    rotation = cycle(('websites', 'mailing-lists', 'newsgroups'))
    with closing(State(tmp_path, identity())) as state:
        state.fill(Frontier(), lambda:None)
        assert state.ready(rotation, size) == identifiers[:size]
        for identifier in identifiers[:size // 2]:
            state.finish(identifier, 'skipped')
        kept = set(identifiers[size // 2:size])
        state.fill(Frontier(), lambda:None)
        assert state.ready(rotation, size, kept) == identifiers[size:]
        assert state.ready(rotation, size) == identifiers[size // 2:]


def test_provider_access_failure_retains_records_and_backs_off(tmp_path):
    import time
    from grading import LunaUnavailable

    service, sources = prepared_service(tmp_path)
    account = budget(tmp_path)
    state = State(tmp_path / 'state', identity())

    class Rejected:
        def request(self, payload):
            raise LunaUnavailable(403, 1800)

        def close(self):
            pass

    def unavailable(*args, client_factory, **kwargs):
        return Enricher(*args, client_factory=lambda _: Rejected(), **kwargs)

    try:
        result = run(tmp_path, state, service, sources, 'fixture', account, maximum=4,
                     should_yield=lambda: False, enricher_factory=unavailable)
        assert result['phase'] == 'retry_wait' and result['next_at'] > time.time() + 1790
        assert result['enrichment']['failed'] == result['enrichment']['updated'] == 0
        assert len(state.ready(cycle(('websites', 'mailing-lists', 'newsgroups')), 4)) == 4
        assert account.snapshot()['unresolved_usd'] > 0
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


def test_historical_enrichment_reuses_only_an_identical_paid_request(tmp_path):
    from contextlib import closing

    grounding = ('EverQuest guild raid report.',)
    first = {'url':'http://guild.example/one', 'archive_path':'websites/first', 'sha256':digest(b'first')}
    second = {'url':'http://guild.example/two', 'archive_path':'websites/second', 'sha256':digest(b'second')}
    calls = []

    class Luna:
        def request(self, payload):
            calls.append(payload)
            return response({**{key: RESULT[key] for key in ('llm_tags','llm_content_flavour','llm_summary')},
                             'content_role':'article', 'topics':[], 'entities':[]})

        def close(self):
            pass

    def open_enricher():
        return closing(Enricher(tmp_path/'scope', 'unused', client_factory=lambda _: Luna()))

    with open_enricher() as client:
        initial = client.enrich_for_maintenance(first, 'First capture', grounding)
    with open_enricher() as client:
        reused = client.enrich_for_maintenance(second, 'Distinct raw capture', grounding)
        assert reused['llm_summary'] == initial['llm_summary']
        assert reused['llm_enrichment_signature'] != initial['llm_enrichment_signature']
        assert client.store.get('enrichment_validation:' + reused['llm_enrichment_signature'])[
            'reused_response_signature'] == initial['llm_enrichment_signature']
        assert len(calls) == 1
        client.enrich_for_maintenance(second, 'Distinct raw capture', ('EverQuest crafting guide.',))
        assert len(calls) == 2
