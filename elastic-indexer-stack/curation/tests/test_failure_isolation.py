"""A retained site failure must never become a worker-wide queue lock."""
import json

import pytest

import automation
from common import CrawlError, digest
from conftest import add_candidate, manifest_for
from jobs import import_name
from review import apply_decisions
from server import create_app
from state import Lane, connect, enqueue, unpack
from test_automation import enable
from test_capture_queue import make_due
from test_server import call
from worker import Worker, ImportQueueUnavailable


def approve_sites(root, rows):
    with connect(root) as store:
        apply_decisions(store, [{'id': row['id'], 'manifest_sha256': row['manifest_sha256'],
                                 'decision': 'approve'} for row in rows], capture_delay=60)
    make_due(root)


@pytest.mark.parametrize('automatic', [False, True])
def test_guildsay_failure_retains_checkpoint_while_next_site_starts(tmp_path, monkeypatch, automatic):
    root = tmp_path / 'state'
    guildsay = add_candidate(root, 'http://www.guildsay.com/')
    next_site = add_candidate(root, 'http://next.example/')
    approve_sites(root, [guildsay, next_site])
    if automatic:
        enable(root)
    def capture(root, batch, sites, progress):
        if sites[0]['id'] == guildsay['id']:
            progress({'files': 8493, 'bytes': 104762352, 'versions_found': 11426,
                      'versions_pending': 2919, 'capture_policy': 'complete-files-v1'})
            raise CrawlError('Wayback connection failed after bounded retries')
        return manifest_for(next_site, batch)
    monkeypatch.setattr('worker.capture_sites', capture)
    worker = Worker(root)
    try:
        worker.capture_queue()
        worker.operation()
        with connect(root) as store:
            failed = dict(store.db.execute('SELECT * FROM operations').fetchone())
        worker.capture_queue()
        worker.operation()
        with connect(root) as store:
            assert store.db.execute('SELECT state FROM candidates WHERE id=?', (next_site['id'],)).fetchone()[0] == 'captured_awaiting_review'
            assert dict(store.db.execute('SELECT * FROM operations WHERE id=?', (failed['id'],)).fetchone()) == failed
            assert store.db.execute('SELECT state FROM candidates WHERE id=?', (guildsay['id'],)).fetchone()[0] == 'capturing'
        app = create_app(root, start_worker=False)
        with connect(root) as store:
            for _ in range(25):
                enqueue(store, 'publish', {})
        detail = call(app, 'GET', '/api/candidate?id=' + guildsay['id']).json()
        assert detail['queue_blocker'] is None
        assert detail['capture_operation']['result']['progress']['files'] == 8493
        listing = call(app, 'GET', '/api/queue?compact=1&filter=capturing').json()
        assert listing['capture_attention']['interrupted'] == 1
        assert failed['id'] not in [operation['id'] for operation in listing['operations']]
        assert listing['candidates'][0]['capture_state'] == 'interrupted'
    finally:
        worker.close()


def test_invalid_capture_approval_is_held_without_rechecking_or_blocking_next(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    broken = add_candidate(root, 'http://broken.example/')
    healthy = add_candidate(root, 'http://healthy.example/')
    approve_sites(root, [broken, healthy])
    source = root / broken['captures'][0]['path']
    source.write_bytes(b'changed after approval')
    worker = Worker(root)
    try:
        worker.capture_queue()
        worker.capture_queue()
        app = create_app(root, start_worker=False)
        failed = call(app, 'GET', '/api/candidate?id=' + broken['id']).json()
        assert failed['candidate']['stage'] == 'queued'
        assert failed['candidate']['capture_queue_error']
        with connect(root) as store:
            op = unpack(store.db.execute('SELECT * FROM operations').fetchone())
            assert op['payload']['sites'][0]['id'] == healthy['id']
        assert call(app, 'POST', '/api/undo', {'id': broken['id'], 'manifest_sha256': broken['manifest_sha256']}).status_code == 200
        restored = call(app, 'GET', '/api/candidate?id=' + broken['id']).json()['candidate']
        assert not restored.get('capture_queue_error')
    finally:
        worker.close()


def test_failed_automatic_site_waits_for_user_while_next_candidate_is_checked(tmp_path):
    root = tmp_path / 'state'
    account = enable(root)
    bad = add_candidate(root, 'http://bad.example/', grade=None)
    good = add_candidate(root, 'http://good.example/', grade=None)
    with connect(root) as store:
        operation = enqueue(store, 'candidate_check', {'id': bad['id'], 'automatic': True})
        store.db.execute("UPDATE operations SET state='interrupted',error='Wayback HTTP 503',result=? WHERE id=?",
                         (json.dumps({'automatic_pause': {'reason': 'transient', 'retry_at': '2000'}}), operation))
        store.db.commit()
    automation.resume_owned(root, Lane.CANDIDATES, account)
    automation.schedule(root, account)
    with connect(root) as store:
        assert store.db.execute('SELECT state FROM operations WHERE id=?', (operation,)).fetchone()[0] == 'interrupted'
        active = unpack(store.db.execute("SELECT * FROM operations WHERE state='queued'").fetchone())
        assert active['payload']['id'] == good['id']


@pytest.mark.parametrize('outcome', ['rejected', 'created_response_lost', 'unknown'])
def test_failed_import_submission_reconciles_jobs_before_advancing(tmp_path, monkeypatch, outcome):
    root = tmp_path / 'state'
    rows = [add_candidate(root, 'http://bad.example/'), add_candidate(root, 'http://good.example/')]
    manifests = [manifest_for(row, digit * 32) for row, digit in zip(rows, 'ab')]
    monkeypatch.setenv('IMPORT_IMAGE', 'dbsanfte/frontend@sha256:' + '1' * 64)
    with connect(root) as store:
        for manifest in manifests:
            store.db.execute("INSERT INTO batches VALUES (?,'published_waiting_index',?,?,?,NULL,NULL,'now','now')",
                             (manifest['batch_id'], json.dumps(manifest), digest(manifest), json.dumps({'commit': 'c' * 40})))
        store.db.execute("UPDATE candidates SET state='published'")
        store.db.commit()
    class Kube:
        def __init__(self): self.items = []; self.calls = []
        def jobs(self):
            if outcome == 'unknown' and self.calls:
                raise OSError('private upstream detail')
            return self.items.copy()
        def create(self, job):
            self.calls.append(job)
            if len(self.calls) == 1:
                if outcome == 'created_response_lost':
                    self.items.append(job)
                raise CrawlError('Import source is unavailable for this site')
            self.items.append(job)
    kube = Kube()
    worker = Worker(root, kube)
    try:
        if outcome == 'unknown':
            with pytest.raises(ImportQueueUnavailable, match='outcome is uncertain'):
                worker.indexing()
            assert len(kube.calls) == 1
            with connect(root) as store:
                assert all(row[0] == 'published_waiting_index' for row in store.db.execute('SELECT state FROM batches'))
            return
        worker.indexing()
        with connect(root) as store:
            batches = [unpack(row) for row in store.db.execute('SELECT * FROM batches ORDER BY id')]
        if outcome == 'created_response_lost':
            assert len(kube.calls) == len(kube.items) == 1
            assert batches[0]['state'] == 'indexing'
            assert batches[1]['state'] == 'published_waiting_index'
            assert batches[1]['job']['waiting_for'] == [kube.items[0]['metadata']['name']]
            return
        assert batches[0]['state'] == 'index_failed'
        assert batches[0]['error'] == 'Import source is unavailable for this site'
        assert batches[0]['job']['name'] == import_name(batches[0])
        assert batches[1]['state'] == 'indexing'
        assert len(kube.items) == 1 and len(kube.calls) == 2
    finally:
        worker.close()


def test_site_failure_cannot_hide_a_later_funded_budget_resume(tmp_path):
    root = tmp_path/'state'
    account = enable(root)
    with connect(root) as store:
        bad = enqueue(store, 'candidate_check', {'automatic': True, 'id': 'a'*24})
        store.db.execute("UPDATE operations SET state='interrupted',error='Source changed' WHERE id=?", (bad,))
        store.db.commit()
        funded = enqueue(store, 'discover', {'automatic': True})
        store.db.execute("UPDATE operations SET state='interrupted',error='Daily budget reached',result=? WHERE id=?",
                         (json.dumps({'automatic_pause': {'reason': 'daily_budget', 'required_usd': .01}}), funded))
        store.db.commit()
    automation.resume_owned(root, Lane.CANDIDATES, account)
    with connect(root) as store:
        assert store.db.execute('SELECT state FROM operations WHERE id=?', (bad,)).fetchone()[0] == 'interrupted'
        assert store.db.execute('SELECT state FROM operations WHERE id=?', (funded,)).fetchone()[0] == 'queued'


def test_candidate_check_preflight_failure_is_held_once_and_other_site_starts(tmp_path, monkeypatch):
    root = tmp_path/'state'
    account = enable(root)
    bad = add_candidate(root, 'http://bad.example/', grade=None)
    good = add_candidate(root, 'http://good.example/', grade=None)
    original = automation.start_check
    calls = []
    def start(store, payload):
        calls.append(payload['id'])
        if payload['id'] == bad['id']:
            raise CrawlError('Site evidence is unavailable')
        return original(store, payload)
    monkeypatch.setattr('automation.start_check', start)
    automation.schedule(root, account)
    automation.schedule(root, account)
    assert calls == [bad['id'], good['id']]
    with connect(root) as store:
        assert store.db.execute('SELECT error FROM candidates WHERE id=?', (bad['id'],)).fetchone()[0] == 'Site evidence is unavailable'
        assert unpack(store.db.execute("SELECT * FROM operations WHERE state='queued'").fetchone())['payload']['id'] == good['id']


def test_failed_retry_discards_old_budget_wakeup_without_losing_progress(candidate, monkeypatch):
    root, row = candidate
    account = enable(root)
    with connect(root) as store:
        identifier = enqueue(store, 'candidate_check', {'id': row['id'], 'automatic': True})
        store.db.execute('UPDATE operations SET result=? WHERE id=?',
                         (json.dumps({'phase':'sampling','automatic_pause':{'reason':'daily_budget','required_usd':.01}}), identifier))
        store.db.commit()
    def fail(*args): raise CrawlError('Source changed')
    monkeypatch.setattr('worker.check_candidate', fail)
    worker = Worker(root)
    try:
        worker.operation(Lane.CANDIDATES)
        automation.resume_owned(root, Lane.CANDIDATES, account)
        with connect(root) as store:
            op = unpack(store.db.execute('SELECT * FROM operations WHERE id=?', (identifier,)).fetchone())
        assert op['state'] == 'interrupted' and op['error'] == 'Source changed'
        assert op['result'] == {'phase':'sampling'}
    finally:
        worker.close()


@pytest.mark.parametrize('failure', ['preflight', 'publication'])
def test_failed_publication_site_stays_visible_while_next_site_advances(tmp_path, monkeypatch, failure):
    from automatic_indexing import prepare_next
    from test_automatic_indexing import completed
    root = tmp_path/'state'
    bad, first = completed(root, 'http://bad.example/', 'a'*32)
    good, second = completed(root, 'http://good.example/', 'b'*32)
    if failure == 'preflight':
        (root / first['captures'][0]['path']).write_bytes(b'changed source')
    prepare_next(root)
    prepare_next(root)
    def publish(root, manifest, expected):
        if manifest['batch_id'] == first['batch_id']:
            raise CrawlError('Archive Git operation failed; staged sources retained')
        return {'commit': 'c'*40}
    monkeypatch.setattr('worker.publish', publish)
    worker = Worker(root)
    try:
        worker.operation(Lane.INDEXING)
        worker.operation(Lane.INDEXING)
        with connect(root) as store:
            failed = unpack(store.db.execute('SELECT * FROM batches WHERE id=?', (first['batch_id'],)).fetchone())
            success = unpack(store.db.execute('SELECT * FROM batches WHERE id=?', (second['batch_id'],)).fetchone())
            assert failed['state'] == ('index_preflight_failed' if failure == 'preflight' else 'publication_requested')
            if failure == 'publication':
                operation = unpack(store.db.execute("SELECT * FROM operations WHERE state='interrupted'").fetchone())
                assert operation['payload']['batch_id'] == first['batch_id']
                assert operation['error'].startswith('Archive Git operation failed')
            assert success['state'] == 'published_waiting_index'
            assert failed['manifest'] == first and success['manifest'] == second
    finally:
        worker.close()
