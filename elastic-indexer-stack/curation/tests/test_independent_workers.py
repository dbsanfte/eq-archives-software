import json
import threading
import time
from types import SimpleNamespace

import pytest

from common import CrawlError, digest
from conftest import add_candidate, manifest_for
from server import create_app
from state import Lane, connect, enqueue, operation_lane, worker_lease
from test_capture_queue import approve, make_due
from test_server import call
from worker import Worker
from wayback_transport import current_transport


@pytest.mark.parametrize('kind', ['capture', 'discover', 'candidate_check'])
@pytest.mark.parametrize('publication_state', ['queued', 'running'])
def test_publication_does_not_block_either_acquisition_worker(candidate, kind, publication_state):
    root, _ = candidate
    with connect(root) as store:
        publication = enqueue(store, 'publish', {'batch_id': 'a' * 32})
        store.db.execute('UPDATE operations SET state=? WHERE id=?', (publication_state, publication))
        store.db.commit()
        acquisition = enqueue(store, kind, {'saved': 'capture bounds'})
        assert acquisition != publication
        with pytest.raises(CrawlError):
            enqueue(store, kind, {})
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 2


@pytest.mark.parametrize('resumed,busy', [(a,b) for a in ('capture','discover','candidate_check','publish')
                                       for b in ('capture','discover','publish') if operation_lane(a) != operation_lane(b)])
def test_resume_only_checks_its_own_worker_and_retains_saved_progress(candidate, resumed, busy):
    root, _ = candidate
    app = create_app(root, start_worker=False)
    payload = {'saved': 'original budget and manifest'}
    progress = {'files': 786}
    with connect(root) as store:
        operation = enqueue(store, resumed, payload)
        store.db.execute("UPDATE operations SET state='interrupted',result=? WHERE id=?", (json.dumps(progress), operation))
        store.db.commit()
        other = enqueue(store, busy, {})
        store.db.execute("UPDATE operations SET state='running' WHERE id=?", (other,))
        store.db.commit()
    response = call(app, 'POST', '/api/resume', {'id': operation})
    assert response.status_code == 202, response.text
    assert call(app, 'POST', '/api/resume', {'id': operation}).status_code == 409
    with connect(root) as store:
        saved = store.db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone()
        assert saved['state'] == 'queued' and json.loads(saved['payload']) == payload
        assert json.loads(saved['result']) == progress
        assert store.db.execute('SELECT state FROM operations WHERE id=?', (other,)).fetchone()[0] == 'running'


@pytest.mark.parametrize('busy', ['capture', 'publish'])
def test_evidence_retry_during_other_work_reuses_the_original_operation_and_cap(candidate, busy):
    root, row = candidate
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET rating=NULL,state='sampled'")
        store.db.commit()
    app = create_app(root, start_worker=False)
    # Fetch the source-bound hash after removing the grade.
    row = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
    payload = {'id': row['id'], 'manifest_sha256': row['manifest_sha256'], 'max_usd': .25}
    first = call(app, 'POST', '/api/check-candidate', payload)
    assert first.status_code == 202
    with connect(root) as store:
        store.db.execute("UPDATE operations SET state='interrupted'")
        store.db.commit()
        enqueue(store, busy, {})
    retry = call(app, 'POST', '/api/check-candidate', {**payload, 'max_usd': 2})
    assert retry.status_code == 202, retry.text
    assert retry.json()['operation'] == first.json()['operation']
    assert retry.json()['max_usd'] == .25


@pytest.mark.parametrize('capture_state', ['queued', 'running', 'interrupted'])
@pytest.mark.parametrize('path,payload', [('/api/discover', {'max_candidates': 50, 'max_usd': 2}),
                                       ('/api/submit-site', {'url': 'http://new.example/', 'max_usd': .25})])
def test_explicit_candidate_actions_remain_available_during_capture_and_publication(candidate, capture_state, path, payload):
    root, _ = candidate
    with connect(root) as store:
        capture = enqueue(store, 'capture', {'retained': 'scope and checkpoint'})
        store.db.execute('UPDATE operations SET state=? WHERE id=?', (capture_state, capture))
        store.db.commit()
        enqueue(store, 'publish', {})
    app = create_app(root, start_worker=False)
    result = call(app, 'POST', path, payload)
    assert result.status_code == 202, result.text
    listing = call(app, 'GET', '/api/queue').json()
    assert listing['workers']['candidates']['id'] == result.json()['operation']
    assert listing['workers']['indexing']['kind'] == 'publish'
    with connect(root) as store:
        assert store.db.execute('SELECT state FROM operations WHERE id=?', (capture,)).fetchone()[0] == capture_state
    assert call(app, 'POST', '/api/discover', {'max_candidates': 50, 'max_usd': 2}).status_code == 409


def test_automatic_capture_claim_and_uncapped_status_ignore_publication_backlog(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    worker = Worker(root)
    try:
        assert approve(app, row).status_code == 200
        make_due(root)
        with connect(root) as store:
            gathering = enqueue(store, 'discover', {'max_candidates': 50, 'max_usd': 2})
            store.db.execute("UPDATE operations SET created='1999' WHERE id=?", (gathering,))
            store.db.commit()
            for _ in range(30):
                enqueue(store, 'publish', {})
        detail = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
        assert detail['queue_blocker'] is None
        worker.capture_queue()
        with connect(root) as store:
            operation = store.db.execute("SELECT id FROM operations WHERE kind='capture'").fetchone()[0]
            # The acquisition may be older than every displayed publication.
            store.db.execute("UPDATE operations SET created='2000' WHERE id=?", (operation,))
            store.db.commit()
        listing = call(app, 'GET', '/api/queue?filter=capturing').json()
        assert listing['stage_counts']['capturing'] == 1
        assert operation not in {item['id'] for item in listing['operations']}
        assert listing['workers']['capture']['id'] == operation
        assert listing['workers']['candidates']['id'] == gathering
        assert listing['workers']['indexing']['kind'] == 'publish'
        worker.capture_queue()
        with connect(root) as store:
            assert store.db.execute("SELECT COUNT(*) FROM operations WHERE kind='capture'").fetchone()[0] == 1
    finally:
        worker.close()


def wait_until(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.01)
    raise AssertionError('Worker did not make progress')


@pytest.mark.parametrize('finishes_first', list(Lane))
def test_all_three_workers_make_independent_progress_and_keep_reindex_job(candidate, monkeypatch, finishes_first):
    root, capture_row = candidate
    published_row = add_candidate(root, url='http://published.example/')
    capture_manifest = manifest_for(capture_row, 'a' * 32)
    published_manifest = manifest_for(published_row, 'b' * 32)
    started = {lane: threading.Event() for lane in Lane}
    release = {lane: threading.Event() for lane in Lane}
    reindex = {'metadata': {'name': 'existing-full-reindex', 'uid': 'unchanged'}, 'spec': {}, 'status': {'active': 1}}
    checked_jobs = threading.Event()

    class FakeKube:
        def jobs(self):
            checked_jobs.set()
            return [reindex]

        def create(self, _):
            raise AssertionError('The existing full reindex must keep the import waiting')

    def capture(_root, _batch, _sites, progress):
        assert current_transport.get() is worker.transport
        progress({'phase': 'downloading', 'files': 1, 'bytes': 84})
        started[Lane.CAPTURE].set()
        assert release[Lane.CAPTURE].wait(10)
        return capture_manifest

    def publish(*_):
        started[Lane.INDEXING].set()
        assert release[Lane.INDEXING].wait(10)
        return {'commit': 'c' * 40, 'marker': 'crawl-manifests/fixture.json'}

    def gather(*_):
        assert current_transport.get() is worker.transport
        started[Lane.CANDIDATES].set()
        assert release[Lane.CANDIDATES].wait(10)
        return {'candidates': 1}

    monkeypatch.setattr('worker.capture_sites', capture)
    monkeypatch.setattr('worker.publish', publish)
    monkeypatch.setattr('worker.campaign', gather)
    worker = Worker(root, kube=FakeKube())
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'capturing',NULL,NULL,NULL,NULL,NULL,'now','now')", (capture_manifest['batch_id'],))
        store.db.execute("INSERT INTO batches VALUES (?,'publication_requested',?,?,NULL,NULL,NULL,'now','now')",
                         (published_manifest['batch_id'], json.dumps(published_manifest), digest(published_manifest)))
        store.db.execute("UPDATE candidates SET state='capturing' WHERE id=?", (capture_row['id'],))
        store.db.execute("UPDATE candidates SET state='approved_waiting_publication' WHERE id=?", (published_row['id'],))
        store.db.commit()
        enqueue(store, 'capture', {'batch_id': capture_manifest['batch_id'], 'sites': capture_manifest['sites']})
        enqueue(store, 'discover', {'max_candidates': 50, 'max_usd': 2})
        enqueue(store, 'publish', {'batch_id': published_manifest['batch_id'], 'manifest_sha256': digest(published_manifest)})
    app = create_app(root, start_worker=True)
    app.state.worker = worker
    try:
        worker.start()
        assert all(event.wait(5) for event in started.values()), 'All workers must start before any finishes'
        assert call(app, 'GET', '/healthz').status_code == 200
        listing = call(app, 'GET', '/api/queue').json()
        assert {lane: status['state'] for lane, status in listing['workers'].items()} == {lane.value: 'running' for lane in Lane}
        # A concurrent claim attempt cannot duplicate work in either lane.
        for lane in Lane:
            worker.operation(lane)
        release[finishes_first].set()
        def completed():
            with connect(root) as store:
                return store.db.execute("SELECT COUNT(*) FROM operations WHERE state='completed'").fetchone()[0] == 1
        wait_until(completed)
        with connect(root) as store:
            batches = {row['id']: dict(row) for row in store.db.execute('SELECT * FROM batches')}
            if finishes_first == Lane.CAPTURE:
                assert batches[capture_manifest['batch_id']]['state'] == 'awaiting_review'
                assert batches[published_manifest['batch_id']]['state'] == 'publication_requested'
            elif finishes_first == Lane.INDEXING:
                assert checked_jobs.wait(5), 'Index reconciliation must run during a long capture'
                assert batches[capture_manifest['batch_id']]['state'] == 'capturing'
        if finishes_first == Lane.INDEXING:
            def waiting_for_reindex():
                with connect(root) as store:
                    job = store.db.execute('SELECT job FROM batches WHERE id=?', (published_manifest['batch_id'],)).fetchone()[0]
                    return job and json.loads(job).get('waiting_for') == ['existing-full-reindex']
            wait_until(waiting_for_reindex)
        assert reindex == {'metadata': {'name': 'existing-full-reindex', 'uid': 'unchanged'}, 'spec': {}, 'status': {'active': 1}}
    finally:
        for event in release.values():
            event.set()
        worker.close()
    assert call(app, 'GET', '/healthz').status_code == 503
    assert not any(thread.is_alive() for thread in worker.threads.values())


def test_restart_retains_all_queues_and_health_requires_all_workers(candidate, monkeypatch):
    root, _ = candidate
    with connect(root) as store:
        capture = enqueue(store, 'capture', {'checkpoint': 'saved'})
        gathering = enqueue(store, 'discover', {'max_candidates': 50, 'max_usd': 2})
        publication = enqueue(store, 'publish', {'manifest_sha256': 'a' * 64})
        store.db.execute("UPDATE operations SET state='running',result=?", (json.dumps({'retained': True}),))
        store.db.commit()
        waiting = enqueue(store, 'publish', {'batch_id': 'b' * 32})
    worker = Worker(root)
    try:
        with connect(root) as store:
            rows = {row['id']: row for row in store.db.execute('SELECT * FROM operations')}
            assert rows[capture]['state'] == rows[publication]['state'] == rows[gathering]['state'] == 'interrupted'
            assert rows[waiting]['state'] == 'queued'
            assert all(json.loads(rows[identifier]['result']) == {'retained': True} for identifier in (capture, publication, gathering))
        app = create_app(root, start_worker=True)
        app.state.worker = worker
        for lane in Lane:
            worker.threads[lane] = SimpleNamespace(is_alive=lambda: True, ident=1, join=lambda timeout: None)
        assert call(app, 'GET', '/healthz').status_code == 200
        for lane in Lane:
            worker.threads[lane].is_alive = lambda: False
            assert call(app, 'GET', '/healthz').status_code == 503
            worker.threads[lane].is_alive = lambda: True
        worker.transport.failed = True
        assert call(app, 'GET', '/healthz').status_code == 503
        worker.transport.failed = False
        # An expired shutdown allowance must not release the process lease
        # while a request in the other worker is still in flight.
        worker.close()
        with pytest.raises(CrawlError, match='Another worker'):
            worker_lease(root)
        for lane in Lane:
            worker.threads[lane].is_alive = lambda: False
        worker.close()
        with worker_lease(root):
            pass
    finally:
        worker.lease.close()
