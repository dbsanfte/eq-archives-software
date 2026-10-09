"""Status reads stay useful despite capped operation lists and never start work."""
import json

from common import now
from server import create_app
from state import connect, enqueue
from test_server import call


def test_activity_survives_old_interrupted_operations_and_does_not_mutate(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    with connect(root) as store:
        discovery = enqueue(store, 'discover', {'automatic': True, 'fill_queue': True})
        result = {'progress': {'accepted': 7, 'checked': 20, 'target': 50,
                              'stop_reason': 'links_exhausted'}, 'private_response': 'must not escape'}
        store.db.execute("UPDATE operations SET state='completed',result=? WHERE id=?", (json.dumps(result), discovery))
        store.db.commit()
        for _ in range(25):
            old = enqueue(store, 'candidate_check', {'id': row['id'], 'max_usd': 2})
            store.db.execute("UPDATE operations SET state='interrupted',updated='2000',created='2000' WHERE id=?", (old,))
            store.db.commit()
        store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                         (row['id'], 'automatic_promoted', '{"secret":"never return raw details"}', now()))
        store.db.commit()
        before = list(store.db.iterdump())
    listing = call(app, 'GET', '/api/queue?filter=queued&compact=1').json()
    assert discovery not in [op['id'] for op in listing['operations']]
    activity = listing['activity']
    assert activity['discovery']['id'] == discovery
    assert activity['discovery']['result'] == {'progress': result['progress']}
    assert any(event['label'] == 'Automatically queued for capture' and event['site']['id'] == row['id']
               for event in activity['recent'])
    assert any(event['id'] == discovery and event['state'] == 'completed' for event in activity['recent'])
    assert len(activity['recent']) <= 8
    assert 'must not escape' not in json.dumps(activity)
    assert 'never return raw details' not in json.dumps(activity)
    call(app, 'GET', '/api/queue?filter=candidates&compact=1')
    with connect(root) as store:
        assert list(store.db.iterdump()) == before


def test_activity_exposes_waiting_import_even_outside_batch_cap_and_queue_grace(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='approved_waiting_batch',decision=? WHERE id=?",
                         (json.dumps({'capture_after': '2099-01-01T00:00:00+00:00'}), row['id']))
        for index in range(110):
            store.db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?,?,?,?)',
                             (f'{index:032x}', 'indexed', '{}', '', None, None, None, '2026', '2026'))
        job = {'waiting_for': ['existing-reindex-job']}
        store.db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?,?,?,?)',
                         ('f'*32, 'published_waiting_index', '{}', '', None, json.dumps(job), 'Kubernetes state unavailable', '2000', '2026'))
        store.db.commit()
    listing = call(app, 'GET', '/api/queue?compact=1').json()
    assert 'f'*32 not in [batch['id'] for batch in listing['batches']]
    assert listing['activity']['indexing']['job'] == job
    assert listing['activity']['next_capture'] == {'id': row['id'], 'url': row['url'], 'ready': '2099-01-01T00:00:00+00:00'}
    assert listing['activity']['indexing']['error'] == 'Kubernetes state unavailable'


def test_empty_activity_and_per_worker_health(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    class Worker:
        stop = type('Stop', (), {'is_set': lambda _: False})()
        transport = type('Transport', (), {'healthy': lambda _: False})()
        from state import Lane
        threads = {Lane.CANDIDATES: type('Thread', (), {'is_alive': lambda _: True})(),
                   Lane.CAPTURE: type('Thread', (), {'is_alive': lambda _: False})(),
                   Lane.INDEXING: type('Thread', (), {'is_alive': lambda _: True})()}
    app.state.worker = Worker()
    listing = call(app, 'GET', '/api/queue').json()
    assert listing['activity']['recent'] == []
    assert listing['activity']['indexing'] is None
    assert listing['activity']['next_capture'] is None
    assert listing['activity']['discovery'] is None
    assert listing['activity']['runtime'] == {'candidates': True, 'capture': False, 'indexing': True, 'wayback': False}
