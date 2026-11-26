import json
import re
import sqlite3
from contextlib import closing, nullcontext
from dataclasses import replace
from itertools import cycle
from threading import Barrier, BrokenBarrierError, Event, Lock
from types import SimpleNamespace

import httpx
import pytest

from common import Store, digest
from daily_budget import DailyBudget, LunaRatePause, attempt_spending
import enrichment_worker
from enrichment_worker import MAX_WORKERS, WORKERS, State, run, write_wave
from import_status import EnrichmentError
from indexer import passages, search_enrichment
from indexer.capture_enrichment import Enricher
from policy_enrichment import eligible, prepare_refresh_hit
from policy_maintenance import UPDATE_BATCH_SIZE, MaintenanceServices, State as PolicyState, identity, repair
from policy_pool import PreparationPool
from policy_scheduler import receipt
from policy_sources import ServiceError, SourceError
from test_enrichment import RESULT, response
from test_policy_enrichment import Maintenance, budget
from test_policy_maintenance import V, fixture


class Service(Maintenance):
    def __init__(self, hits):
        super().__init__(hits)
        self.waves = []

    def select_enrichment(self, cursor, *, collection, limit):
        return [key for key, hit in sorted(self.hits.items()) if key > cursor and key.startswith(collection + '/')
                and eligible(hit['_source'])][:limit]

    def vectors(self, inputs, check):
        self.waves.append(list(inputs))
        return super().vectors(inputs, check)

    def update_many(self, updates):
        for hit, patch in updates:
            self.update(hit, patch)
        return [None] * len(updates)


def test_write_wave_bounds_batches_and_acknowledges_only_verified_items():
    records = [(SimpleNamespace(hit={'_id': str(index)}), None) for index in range(UPDATE_BATCH_SIZE + 1)]
    sent = []
    checks = []

    class Writer:
        def update_many(self, updates):
            sent.append([hit['_id'] for hit, _ in updates])
            assert all(patch == {'llm_enrichment_passage_version': passages.VERSION} for _, patch in updates)
            return [None] * len(updates)

    assert list(write_wave(Writer(), records, lambda: checks.append(1))) == [
        (str(index), 'reused') for index in range(UPDATE_BATCH_SIZE + 1)]
    assert list(map(len, sent)) == [UPDATE_BATCH_SIZE, 1] and len(checks) == 2

    class Interrupted(Writer):
        def update_many(self, updates):
            super().update_many(updates)
            return [ServiceError('elasticsearch_unavailable'), SourceError('concurrent_update'), None]

    results = write_wave(Interrupted(), records[:3], lambda: None)
    assert next(results) == ('1', 'concurrent_update')
    assert next(results) == ('2', 'reused')
    with pytest.raises(ServiceError, match='elasticsearch_unavailable'):
        next(results)


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
    for index, identifier in enumerate(identifiers[:count], 1):
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
                try:
                    barrier.wait()
                except BrokenBarrierError as error:
                    raise AssertionError(f'{len(calls)} of {barrier.parties} requests reached Luna') from error
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


def test_prepared_enrichment_reaches_the_optimistic_bulk_writer(tmp_path):
    service, sources = prepared_service(tmp_path, count=1)
    hit, = service.hits.values()
    backend = MaintenanceServices.__new__(MaintenanceServices)
    backend.index, backend.es_url = hit['_index'], 'https://archive'
    calls = []

    def respond(request):
        action, payload = [json.loads(line) for line in request.content.splitlines()]
        assert request.url.path == f'/{backend.index}/_bulk'
        assert action == {'update': {'_id': hit['_id'], 'if_seq_no': hit['_seq_no'], 'if_primary_term': hit['_primary_term']}}
        assert payload['doc']['llm_enrichment_passage_version'] == passages.VERSION
        assert len(payload['doc']['llm_summary_vector']) == 768
        calls.append(action)
        return httpx.Response(200, json={'errors': False, 'items': [{'update': {
            '_id': hit['_id'], '_index': backend.index, 'status': 200, 'result': 'updated'}}]})

    with closing(sources), closing(State(tmp_path / 'state', identity())) as state, \
            httpx.Client(transport=httpx.MockTransport(respond)) as backend.client, \
            closing(PreparationPool(sources.roots, sources.publication, workers=1, enrichment=True)) as pool:
        service.update_many = backend.update_many
        result = run(tmp_path, state, service, sources, 'fixture', budget(tmp_path), maximum=1,
                     enricher_factory=factory([]), should_yield=lambda: False, preparation_pool=pool)
    assert len(calls) == result['updated'] == 1


@pytest.mark.parametrize('maximum', [9, 60])
def test_scope_reservations_release_under_concurrent_enrichment(tmp_path, monkeypatch, maximum):
    service, sources = prepared_service(tmp_path, count=9)
    account = budget(tmp_path, daily=10)
    first_request, other_scope, budget_denied = Event(), Event(), Event()
    scope = ('websites', 'guild.example', None)
    directory = tmp_path / 'state' / 'policy-enrichment' / digest(scope)
    with closing(Store(directory)) as store, store.db:
        store.db.execute('''INSERT INTO attempts(candidate,signature,reserved,status,created,actual)
                            VALUES ('prior','settled',49.96,'received','2026-11-26T00:00:00Z',49.96)''')

    original = Enricher.enrich_for_maintenance

    def observe_budget(enricher, *args, **kwargs):
        try:
            return original(enricher, *args, **kwargs)
        except EnrichmentError as error:
            if error.code == 'budget':
                budget_denied.set()
            raise

    def await_reservations(number):
        if number in (2, 3):
            other_scope.set()
        elif not first_request.is_set():
            first_request.set()
            assert budget_denied.wait(5), 'expected overlapping requests to fill the scope allowance'
            assert other_scope.wait(5), 'another scope could not make progress'

    monkeypatch.setattr(Enricher, 'enrich_for_maintenance', observe_budget)
    calls = []
    root = tmp_path / 'state'
    enrich = factory(calls, on_request=await_reservations)
    preparation = (closing(PreparationPool(sources.roots, sources.publication, enrichment=True))
                   if maximum == 60 else nullcontext())
    with closing(sources), preparation as pool:
        with closing(State(root, identity())) as state:
            result = run(tmp_path, state, service, sources, 'fixture', account, maximum=maximum, workers=8,
                         should_yield=lambda: False, enricher_factory=enrich, preparation_pool=pool)
            assert result['execution']['scope_budget_retries'] > 0
            assert result['failed'] == result['skipped'] == 0
            if maximum == 9:
                attempts = state.db.connection().execute('SELECT attempts FROM pending').fetchall()
                assert attempts and all(row == (0,) for row in attempts)
            else:
                assert not state.ready(cycle(enrichment_worker.COLLECTIONS), 60)
        if maximum == 9:
            with closing(State(root, identity())) as state:
                result = run(tmp_path, state, service, sources, 'resumed', account, maximum=60, workers=8,
                             should_yield=lambda: False, enricher_factory=enrich)
                assert not state.ready(cycle(enrichment_worker.COLLECTIONS), 60)
    assert budget_denied.is_set()
    assert len(calls) == result['updated'] == 9
    assert result['failed'] == result['skipped'] == 0
    with closing(Store(directory, initialize=False)) as store:
        spent = attempt_spending(store)
    assert 49.96 < spent['estimated_usd'] < 50 and spent['reserved_usd'] == 0


def test_scope_exhaustion_keeps_uncertain_charges_and_other_scopes_running(tmp_path):
    service, sources = prepared_service(tmp_path, count=9)
    root = tmp_path / 'state'
    directory = root / 'policy-enrichment' / digest(('websites', 'guild.example', None))
    with closing(Store(directory)) as store, store.db:
        store.db.executemany('''INSERT INTO attempts(candidate,signature,reserved,status,created,actual)
                              VALUES ('prior',?,?,?,'2026-11-26T00:00:00Z',?)''',
                             [('settled', 49.90, 'received', 49.90), ('uncertain', .09, 'reserved', None)])
    requests = []
    account = budget(tmp_path, daily=10)
    with closing(sources), closing(State(root, identity())) as state:
        result = run(tmp_path, state, service, sources, 'fixture', account, maximum=60, workers=8,
                     should_yield=lambda: False, enricher_factory=factory([], on_request=requests.append))
        assert not state.ready(cycle(enrichment_worker.COLLECTIONS), 60)
    assert sorted(requests) == [2, 3]
    assert result['updated'] == 2 and result['skipped'] == 7 and result['failed'] == 0
    assert result['execution']['scope_budget_retries'] > 0
    assert result['execution']['scope_budget_exhausted'] == 1
    with closing(Store(directory, initialize=False)) as store:
        assert attempt_spending(store) == {'estimated_usd': 49.90, 'reserved_usd': .09}


def test_held_newsgroup_cannot_fill_the_next_enrichment_slice(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    group = 'alt.games.everquest'
    directory = root / 'policy-enrichment' / digest(('newsgroups', group))
    with closing(Store(directory)) as store, store.db:
        store.db.execute('''INSERT INTO attempts(candidate,signature,reserved,status,created,actual)
                         VALUES ('prior','held',50,'reserved','2026-11-26T00:00:00Z',NULL)''')

    size = enrichment_worker.FRONTIER_PAGE_SIZE
    held = [f'newsgroups/{group}-msg-{index:04d}.txt' for index in range(3 * size)]
    other = [f'newsgroups/{group}-msg-z.txt', 'newsgroups/alt.games.everquest.extra-msg-1.txt']
    service, sources = prepared_service(tmp_path, count=3)
    hit, = (hit for identifier, hit in service.hits.items() if identifier.startswith('newsgroups/'))

    class Frontier(Service):
        def __init__(self):
            super().__init__({**hit, '_id': key, '_source': {**hit['_source'], 'id': key}}
                             for key in held + other)
            self.filtered_pages = []

        def select_enrichment(self, cursor, *, collection, limit, exclude_newsgroups=()):
            page = super().select_enrichment(cursor, collection=collection, limit=len(self.hits))
            if exclude_newsgroups:
                assert collection == 'newsgroups'
                pattern = '|'.join(fr'newsgroups/{re.escape(group)}-msg-[0-9]+\.txt'
                                   for group in exclude_newsgroups)
                page = [key for key in page if not re.fullmatch(pattern, key)]
                self.filtered_pages.append(page[:limit])
            return page[:limit]

    # Use the real budget logic with fixtures that share equivalent archive text.
    def prepare(services, sources, identifier, url, check):
        return replace(prepare_refresh_hit(hit, sources, url), hit=services.get(identifier))

    monkeypatch.setattr(enrichment_worker, 'prepare_refresh', prepare)
    frontier = Frontier()
    calls = []
    with closing(sources), closing(State(root, identity())) as state:
        result = run(tmp_path, state, frontier, sources, 'held', budget(tmp_path, daily=10),
                     maximum=12, workers=1, enricher_factory=factory(calls), should_yield=lambda: False)
        assert result['updated'] == len(other)
        assert result['failed'] == 0 and result['skipped'] == size
        assert result['execution']['examined'] == 1 + len(other)
        assert result['execution']['scope_budget_bypassed'] == size - 1
        assert frontier.filtered_pages == [other, []]
        assert result['next_at'] > result['updated_at'] + 600
        assert len(calls) == len(other)
    with closing(State(root, identity())) as resumed:
        # A new slice with a fresh budget decision can still reach the hidden tail.
        resumed.fill(frontier, lambda: None)
        assert resumed.ready(cycle(enrichment_worker.COLLECTIONS), 1) == [held[size]]


def test_scope_filter_keeps_existing_queue_and_canonical_aliases(tmp_path):
    key = identity() + ':' + search_enrichment.VERSION
    website = 'websites/guild.example/20000101000000/page.html'
    alias = 'websites/www.guild.example:80/20000101000000/another.html'
    other = 'websites/other.example/20000101000000/news.html'
    identifiers = [website, alias, other]
    with closing(sqlite3.connect(tmp_path / 'enrichment-maintenance.sqlite')) as old, old:
        old.execute('''CREATE TABLE pending (key TEXT, collection TEXT, id TEXT, attempts INTEGER DEFAULT 0,
                    retry_at REAL DEFAULT 0, PRIMARY KEY(key,id))''')
        old.executemany('INSERT INTO pending VALUES (?,\'websites\',?,2,0)', [(key, path) for path in identifiers])

    with closing(State(tmp_path, identity())) as state:
        scope = digest(('websites', 'guild.example', None))
        rotation = cycle(enrichment_worker.COLLECTIONS)
        assert state.ready(rotation, 4, blocked_scopes=(scope,)) == [other]
        assert state.ready(rotation, 4) == sorted(identifiers)
        assert state.db.connection().execute('SELECT DISTINCT attempts FROM pending').fetchall() == [(2,)]


@pytest.mark.parametrize('worker_count', [WORKERS, MAX_WORKERS])
def test_paid_requests_overlap_at_default_and_maximum_concurrency(tmp_path, worker_count, monkeypatch):
    service, sources = prepared_service(tmp_path, count=worker_count)
    account = budget(tmp_path, daily=10)
    state = State(tmp_path / 'indexing-state', identity())
    calls = []
    initialized = []
    install = enrichment_worker.install_scope_totals
    original_enrichment = Enricher.enrich_for_maintenance
    problems = []

    def record_failure(*args, **kwargs):
        try:
            return original_enrichment(*args, **kwargs)
        except Exception as error:
            problems.append((type(error).__name__, str(error)))
            raise

    monkeypatch.setattr(Enricher, 'enrich_for_maintenance', record_failure)

    def record_initialization(db):
        initialized.append(db.execute('PRAGMA database_list').fetchone()[2])
        install(db)

    monkeypatch.setattr(enrichment_worker, 'install_scope_totals', record_initialization)
    try:
        job = 'eqarchives-enrichment-' + identity() + '-1'
        with closing(PreparationPool(sources.roots, sources.publication, enrichment=True)) as pool:
            result = run(tmp_path,state,service,sources,job,account,maximum=worker_count,should_yield=lambda:False,
                         workers=worker_count, enricher_factory=factory(calls, Barrier(worker_count, timeout=20)),
                         preparation_pool=pool)
        scheduler_state = {'name':job, **{key:result[key] for key in ('image','policy','key')}}
        assert receipt(tmp_path, scheduler_state)['execution'] == result['execution']
        assert result['execution']['preparation_workers'] == 2
        assert result['execution']['checkpoint_views'] == worker_count
        assert result['execution']['rebuilt_views'] == 0
        assert result['enrichment']['updated'] == worker_count, (len(calls), sorted(set(problems)), result)
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


def test_ready_source_is_graded_while_an_earlier_source_is_still_preparing(tmp_path):
    service, sources = prepared_service(tmp_path, count=3)
    account = budget(tmp_path)
    graded = []

    class Task:
        def __init__(self, hit, url):
            self.hit, self.url = hit, url

        def ready(self):
            return not self.hit['_id'].endswith('page-one.html') or bool(graded)

        def result(self, check):
            check()
            return prepare_refresh_hit(self.hit, sources, self.url)

    class Pool:
        workers = 2

        def submit(self, task):
            return Task(*task)

        def cancel_pending(self):
            pass

    with closing(sources), closing(State(tmp_path / 'state', identity())) as state:
        result = run(tmp_path, state, service, sources, 'fixture', account, maximum=3, workers=1,
                     should_yield=lambda: False, preparation_pool=Pool(),
                     enricher_factory=factory([], on_request=graded.append), seconds=25)
    assert len(graded) == 3 and graded[0] == 2
    assert result['updated'] == 3


def test_lost_preparation_defers_only_its_record_and_runs_later_sources(tmp_path):
    service, sources = prepared_service(tmp_path, count=3)
    account = budget(tmp_path)
    lost_id = 'websites/guild.example/20000101000000/page-one.html'

    class Pool(PreparationPool):
        def submit(self, task):
            pending = super().submit(task)
            if task[0]['_id'] == lost_id:
                pending.slot.process.kill()
                pending.slot.process.join(2)
            return pending

    with closing(sources), closing(State(tmp_path / 'state', identity())) as state, \
            closing(Pool(sources.roots, sources.publication, enrichment=True)) as pool:
        result = run(tmp_path, state, service, sources, 'fixture', account, maximum=3,
                     should_yield=lambda: False, preparation_pool=pool, enricher_factory=factory([]))
        pending = state.db.connection().execute('SELECT id,attempts FROM pending WHERE key=?', (state.key,)).fetchall()
    assert pending == [(lost_id, 1)]
    assert result['updated'] == 2 and result['failed'] == 0
    assert result['execution']['preparation_retries'] == 1


def test_foreground_yield_cancels_source_prefetch_and_preserves_queue(tmp_path):
    service, sources = prepared_service(tmp_path, count=3)
    account = budget(tmp_path)
    graded = []
    with closing(sources), closing(State(tmp_path / 'state', identity())) as state, \
            closing(PreparationPool(sources.roots, sources.publication, enrichment=True)) as pool:
        result = run(tmp_path, state, service, sources, 'fixture', account, maximum=3,
                     should_yield=lambda: bool(pool.slots), preparation_pool=pool, enricher_factory=factory(graded))
        pending = state.db.connection().execute('SELECT attempts FROM pending WHERE key=?', (state.key,)).fetchall()
        assert pending == [(0,), (0,), (0,)] and pool.slots == []
    assert result['phase'] == 'yielded' and result['execution']['examined'] == 1
    assert not graded and not service.updates


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
    size = enrichment_worker.FRONTIER_PAGE_SIZE
    retained = enrichment_worker.FRONTIER_REFILL_AT
    identifiers = [f'websites/guild.example/20000101000000/page-{number:03d}'
                   for number in range(2 * size - retained)]

    class Frontier:
        def select_enrichment(self, cursor, *, collection, limit):
            return [key for key in identifiers if key.startswith(collection + '/') and key > cursor][:limit]

    rotation = cycle(('websites', 'mailing-lists', 'newsgroups'))
    with closing(State(tmp_path, identity())) as state:
        assert state.fill(Frontier(), lambda:None) == 3
        assert state.ready(rotation, size) == identifiers[:size]
        for identifier in identifiers[:size - retained]:
            state.finish(identifier, 'skipped')
        kept = set(identifiers[size - retained:size])
        assert state.fill(Frontier(), lambda:None) == 1
        assert state.ready(rotation, size, kept) == identifiers[size:]
        assert state.ready(rotation, size) == identifiers[size - retained:]
    with closing(State(tmp_path, identity())) as state:
        assert state.fill(Frontier(), lambda:None) == 0
        assert state.ready(rotation, size) == identifiers[size - retained:]


def test_ready_refill_keeps_round_robin_and_skips_deferred_and_active_rows(tmp_path):
    collections = enrichment_worker.COLLECTIONS
    size = enrichment_worker.FRONTIER_PAGE_SIZE
    identifiers = {name: [f'{name}/{number:04d}' for number in range(size)] for name in collections}

    class Frontier:
        def select_enrichment(self, cursor, *, collection, limit):
            return [identifier for identifier in identifiers[collection] if identifier > cursor][:limit]

    with closing(State(tmp_path, identity())) as state:
        state.fill(Frontier(), lambda:None)
        initial = [identifiers[collection][0] for collection in collections]
        state.defer(identifiers[collections[0]][1])
        assert state.ready(cycle(collections), 3, set(initial)) == [
            identifiers[collections[0]][2], identifiers[collections[1]][1], identifiers[collections[2]][1]]


def test_failed_frontier_page_keeps_its_cursor_and_pending_work(tmp_path):
    class Frontier:
        def select_enrichment(self, cursor, *, collection, limit):
            raise ServiceError('elasticsearch_unavailable')

    with closing(State(tmp_path, identity())) as state:
        collection = 'mailing-lists'
        pending = 'mailing-lists/guild/0001'
        db = state.db.connection()
        with state.db.transaction() as transaction:
            transaction.execute('UPDATE frontiers SET cursor=? WHERE key=? AND collection=?',
                                (pending, state.key, collection))
            transaction.execute('INSERT INTO pending(key,collection,id) VALUES (?,?,?)',
                                (state.key, collection, pending))
        with pytest.raises(ServiceError, match='elasticsearch_unavailable'):
            state.fill(Frontier(), lambda:None)
        cursor, = db.execute('SELECT cursor FROM frontiers WHERE key=? AND collection=?',
                             (state.key, collection)).fetchone()
        assert cursor == pending
        assert state.ready(cycle(enrichment_worker.COLLECTIONS), 1) == [pending]


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


def test_historical_cooldown_leaves_queue_unadmitted_and_resumes(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    service, sources = prepared_service(tmp_path)
    account = budget(tmp_path)
    stamp = datetime.now(timezone.utc)
    monkeypatch.setattr('daily_budget.instant', lambda: stamp)
    account.observe_luna_capacity(stamp, 15, {})
    calls = []
    with closing(sources), closing(State(tmp_path / 'state', identity())) as state:
        def attempt():
            return run(tmp_path, state, service, sources, 'fixture', account, maximum=4,
                       should_yield=lambda: False, enricher_factory=factory(calls))

        first = attempt()
        assert first['phase'] == 'retry_wait' and first['execution']['examined'] == 0 and not calls
        assert first['next_at'] == (stamp + timedelta(seconds=15)).timestamp()
        stamp += timedelta(seconds=16)
        assert attempt()['updated'] == 4 and len(calls) == 4


def test_known_luna_throttle_keeps_reservation_and_can_resume_same_request(tmp_path):
    from datetime import datetime, timezone

    account = budget(tmp_path)
    grounding = ('EverQuest guild raid report.',)
    capture = {'url': 'http://guild.example/news', 'archive_path': 'websites/guild.example/20000101000000/news',
               'sha256': digest(b'archive source')}
    calls = []

    class Provider:
        def request(self, payload):
            calls.append(payload)
            if len(calls) <= 2:
                raise LunaRatePause(datetime.now(timezone.utc), status=429)
            return response({**{key: RESULT[key] for key in ('llm_tags', 'llm_content_flavour')},
                             'llm_summary': 'Guild raid report.', 'content_role': 'article', 'topics': [], 'entities': []})

        def close(self):
            pass

    with account.bind(purpose='maintenance'), closing(Enricher(tmp_path / 'cache', 'unused', client_factory=lambda _: Provider())) as enricher:
        for _ in range(2):
            with pytest.raises(LunaRatePause):
                enricher.enrich_for_maintenance(capture, grounding[0], grounding)
        assert enricher.enrich_for_maintenance(capture, grounding[0], grounding)['llm_summary'] == 'Guild raid report.'
        assert calls[0] == calls[1] == calls[2]
        assert enricher.store.db.execute("SELECT COUNT(*) FROM attempts WHERE status='provider_throttled' AND actual IS NULL").fetchone()[0] == 2
        assert account.snapshot(maintenance=True)['unresolved_usd'] > 0


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
