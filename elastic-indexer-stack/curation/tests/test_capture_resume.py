"""An explicit retry joins the FIFO queue without taking over its worker."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import automation
from capture_queue import claim
from common import digest
from conftest import add_candidate
from server import create_app
from state import Lane, connect, enqueue, unpack
from test_capture_queue import approve, make_due
from test_server import call
from worker import Worker


def interrupted(root, app, row, *, automatic=False):
    assert approve(app, row).status_code == 200
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        operation, batch = claim(store, [row['id']])
        payload = unpack(store.db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone())['payload']
        if automatic:
            payload['automatic'] = True
        progress = {'batch_id': batch, 'progress': {'files': 12, 'bytes': 9876, 'urls_checked': 15,
                    'capture_policy': 'complete-files-v1'}, 'retained_budget': {'requests': 32, 'bytes': 100000}}
        store.db.execute("UPDATE operations SET state='interrupted',payload=?,result=?,error='Wayback connection failed after bounded retries' WHERE id=?",
                         (json.dumps(payload), json.dumps(progress), operation))
        store.db.commit()
    return operation, payload, progress


@pytest.mark.parametrize('automatic', [False, True])
def test_resume_joins_queue_during_other_capture_and_keeps_checkpoint(candidate, automatic):
    root, row = candidate
    app = create_app(root, start_worker=False)
    op, payload, progress = interrupted(root, app, row, automatic=automatic)
    account = automation.budget(root)
    account.configure({'enabled': automatic, 'daily_usd': 2, 'min_grade': 2, 'grading_criteria': '', 'revision': 0})
    worker = Worker(root)
    try:
        other = add_candidate(root, url='http://other.example/')
        assert approve(app, other).status_code == 200
        make_due(root)
        worker.capture_queue()
        before = call(app, 'GET', '/api/queue?filter=all').json()['workers']['capture']
        source = root / row['captures'][0]['path']
        original = digest(source.read_bytes())
        reply = call(app, 'POST', '/api/resume', {'id': op})
        assert reply.status_code == 202, reply.text
        assert reply.json()['state'] == 'resume_queued'
        assert call(app, 'POST', '/api/resume', {'id': op}).status_code == 409
        queued = call(app, 'GET', '/api/queue?filter=queued').json()
        assert queued['stage_counts']['queued'] == queued['approved'] == 1
        assert queued['stage_counts']['capturing'] == 1
        assert queued['candidates'][0]['state'] == 'capture_resume_queued'
        assert queued['candidates'][0]['capture_operation_id'] == op
        assert queued['workers']['capture'] == before
        assert queued['capture_attention']['interrupted'] == 0
        worker.capture_queue()
        with connect(root) as store:
            saved = unpack(store.db.execute('SELECT * FROM operations WHERE id=?', (op,)).fetchone())
            assert saved['state'] == 'resume_queued'
            assert saved['payload'] == payload and saved['result'] == progress
            assert saved['error'] == 'Wayback connection failed after bounded retries'
        assert digest(source.read_bytes()) == original
        assert call(app, 'GET', '/api/candidate?id=' + row['id']).json()['queue_position'] == 1
    finally:
        worker.close()


def test_resumes_share_fifo_with_approvals_survive_restart_and_cancel(candidate, monkeypatch):
    root, row = candidate
    app = create_app(root, start_worker=False)
    op, payload, progress = interrupted(root, app, row)
    older = add_candidate(root, url='http://older.example/')
    assert approve(app, older).status_code == 200
    make_due(root)
    assert call(app, 'POST', '/api/resume', {'id': op}).status_code == 202
    newer = add_candidate(root, url='http://newer.example/')
    assert approve(app, newer).status_code == 200
    worker = Worker(root)
    try:
        listing = call(app, 'GET', '/api/queue?filter=approved').json()
        assert [site['id'] for site in listing['candidates']] == [older['id'], row['id'], newer['id']]
        worker.capture_queue()
        with connect(root) as store:
            active = store.db.execute("SELECT * FROM operations WHERE state='queued'").fetchone()
            assert json.loads(active['payload'])['sites'][0]['id'] == older['id']
            store.db.execute("UPDATE operations SET state='interrupted' WHERE id=?", (active['id'],))
            store.db.commit()
        worker.capture_queue()
        detail = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
        assert detail['candidate']['stage'] == 'capturing'
        assert detail['capture_operation']['id'] == op
        assert detail['capture_operation']['payload'] == payload
        assert detail['capture_operation']['result'] == progress
        assert call(app, 'POST', '/api/cancel-resume', {'id': op}).status_code == 409
        calls = []
        def capture(root_, batch, sites, progress):
            calls.append((batch, sites))
            raise RuntimeError('still unavailable')
        monkeypatch.setattr('worker.capture_sites', capture)
        worker.operation()
        assert calls == [(payload['batch_id'], payload['sites'])]
        assert call(app, 'POST', '/api/resume', {'id': op}).status_code == 202
        assert call(app, 'POST', '/api/cancel-resume', {'id': op}).status_code == 200
        paused = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
        assert paused['candidate']['stage'] == 'capturing'
        assert paused['capture_operation']['state'] == 'interrupted'
        assert paused['capture_operation']['result'] == progress
        assert call(app, 'POST', '/api/cancel-resume', {'id': op}).status_code == 409
        assert call(app, 'POST', '/api/undo', {'id': row['id'], 'manifest_sha256': row['manifest_sha256']}).status_code == 409
    finally:
        worker.close()


def test_cancel_and_claim_have_one_winner(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    op, _, _ = interrupted(root, app, row)
    call(app, 'POST', '/api/resume', {'id': op})
    worker = Worker(root)
    barrier = threading.Barrier(2)
    def cancel():
        barrier.wait()
        return call(app, 'POST', '/api/cancel-resume', {'id': op}).status_code
    def start():
        barrier.wait()
        worker.capture_queue()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancelled = pool.submit(cancel)
            started = pool.submit(start)
            code = cancelled.result(); started.result()
        current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
        assert (code, current['capture_operation']['state']) in [(200, 'interrupted'), (409, 'queued')]
        assert current['candidate']['state'] == 'capturing'
    finally:
        worker.close()


def test_unbounded_resume_queue_and_stale_capture_rejection(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    rows = [row] + [add_candidate(root, url=f'http://retry{n}.example/') for n in range(11)]
    operations = [interrupted(root, app, item)[0] for item in rows]
    for operation in operations:
        assert call(app, 'POST', '/api/resume', {'id': operation}).status_code == 202
    assert call(app, 'GET', '/api/queue?filter=queued').json()['total'] == 12
    assert call(app, 'POST', '/api/cancel-resume', {'id': operations[0]}).status_code == 200
    with connect(root) as store:
        newer = enqueue(store, 'capture', {'sites': [{'id': row['id']}]})
        store.db.execute("UPDATE operations SET state='interrupted' WHERE id=?", (newer,))
        store.db.commit()
    response = call(app, 'POST', '/api/resume', {'id': operations[0]})
    assert response.status_code == 409
    assert call(app, 'GET', '/api/queue?filter=queued').json()['total'] == 11


def test_cancel_automatic_restart_resume_requires_another_human_resume(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    op, _, _ = interrupted(root, app, row, automatic=True)
    account = automation.budget(root)
    account.configure({'enabled': True, 'daily_usd': 2, 'min_grade': 2, 'grading_criteria': '', 'revision': 0})
    with connect(root) as store:
        store.db.execute("UPDATE operations SET error='Worker stopped; resume explicitly' WHERE id=?", (op,))
        store.db.commit()
    assert call(app, 'POST', '/api/resume', {'id': op}).status_code == 202
    assert call(app, 'POST', '/api/cancel-resume', {'id': op}).status_code == 200
    automation.resume_owned(root, Lane.CAPTURE, account)
    assert call(app, 'GET', '/api/candidate?id=' + row['id']).json()['capture_operation']['state'] == 'interrupted'


def test_legacy_multi_site_resume_and_cancel_are_atomic(candidate):
    root, first = candidate
    second = add_candidate(root, url='http://second.example/')
    app = create_app(root, start_worker=False)
    for row in (first, second):
        assert approve(app, row).status_code == 200
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        operation, batch = claim(store, [first['id'], second['id']])
        store.db.execute("UPDATE operations SET state='interrupted' WHERE id=?", (operation,))
        original = dict(store.db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone())
        store.db.commit()
    assert call(app, 'POST', '/api/resume', {'id': operation}).status_code == 202
    assert call(app, 'GET', '/api/queue?filter=queued').json()['total'] == 2
    assert call(app, 'POST', '/api/cancel-resume', {'id': operation}).status_code == 200
    assert call(app, 'GET', '/api/queue?filter=capturing').json()['total'] == 2
    assert call(app, 'POST', '/api/resume', {'id': operation}).status_code == 202
    worker = Worker(root)
    try:
        worker.capture_queue()
        with connect(root) as store:
            current = dict(store.db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone())
            assert current['payload'] == original['payload']
            assert current['state'] == 'queued'
            assert store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0] == 1
        assert call(app, 'GET', '/api/queue?filter=capturing').json()['total'] == 2
    finally:
        worker.close()


def test_stale_queued_resume_cannot_hold_next_site(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    op, _, _ = interrupted(root, app, row)
    call(app, 'POST', '/api/resume', {'id': op})
    other = add_candidate(root, url='http://next.example/')
    approve(app, other)
    # Simulate corrupt/stale membership in the saved operation after queueing.
    with connect(root) as store:
        saved = unpack(store.db.execute('SELECT * FROM operations WHERE id=?', (op,)).fetchone())
        saved['payload']['sites'].append({'id': 'missing'})
        store.db.execute('UPDATE operations SET payload=?,updated=? WHERE id=?', (json.dumps(saved['payload']), '1999', op))
        store.db.commit()
    make_due(root)
    worker = Worker(root)
    try:
        worker.capture_queue()
        paused = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
        assert paused['capture_operation']['state'] == 'interrupted'
        assert 'Capture changed' in paused['capture_operation']['error']
        assert paused['candidate']['stage'] == 'capturing'
        worker.capture_queue()
        assert call(app, 'GET', '/api/candidate?id=' + other['id']).json()['capture_operation']['state'] == 'queued'
    finally:
        worker.close()


@pytest.mark.parametrize('payload', [{}, {'id': 'not-an-id'}, {'id': []}])
def test_cancel_requires_a_current_operation_id(candidate, payload):
    root, _ = candidate
    assert call(create_app(root, start_worker=False), 'POST', '/api/cancel-resume', payload).status_code == 409


def test_resume_rejects_empty_site_payload(candidate):
    root, _ = candidate
    with connect(root) as store:
        operation = enqueue(store, 'capture', {'sites': []})
        store.db.execute("UPDATE operations SET state='interrupted'")
        store.db.commit()
    response = call(create_app(root, start_worker=False), 'POST', '/api/resume', {'id': operation})
    assert response.status_code == 409 and 'original sites' in response.text


def test_interrupted_regeneration_resumes_without_undoing_its_predecessor(tmp_path):
    from test_capture_continuation import legacy, regenerate
    root = tmp_path / 'state'
    row, manifest = legacy(root)
    app = create_app(root, start_worker=False)
    assert regenerate(app, manifest).status_code == 202
    worker = Worker(root)
    try:
        make_due(root)
        worker.capture_queue()
        original = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
        op = original['capture_operation']['id']
        with connect(root) as store:
            store.db.execute("UPDATE operations SET state='interrupted' WHERE id=?", (op,))
            store.db.commit()
        assert call(app, 'POST', '/api/resume', {'id': op}).status_code == 202
        queued = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
        assert queued['candidate']['stage'] == 'queued'
        assert queued['candidate']['review_state'] == 'capture_continued'
        assert queued['capture_operation']['payload'] == original['capture_operation']['payload']
        assert call(app, 'POST', '/api/cancel-resume', {'id': op}).status_code == 200
        cancelled = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
        assert cancelled['candidate']['stage'] == 'capturing'
        assert cancelled['candidate']['coverage'] == original['candidate']['coverage']
        with connect(root) as store:
            predecessor = unpack(store.db.execute('SELECT * FROM batches WHERE id=?', (manifest['batch_id'],)).fetchone())
            assert predecessor['state'] == 'capture_continued' and predecessor['manifest'] == manifest
    finally:
        worker.close()
