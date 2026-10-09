from datetime import datetime, timedelta, timezone
import copy
import json
from unittest.mock import patch

import pytest

import automation
from common import CrawlError, digest, now
from conftest import add_candidate
from daily_budget import DailyBudgetPause, AutomaticStopped
from review import record
from server import create_app
from state import Lane, connect, enqueue, unpack
from test_capture_queue import make_due
from test_daily_budget import settings
from test_server import call
from worker import Worker


def enable(root, **changes):
    account = automation.budget(root)
    account.configure(settings(account, enabled=True, **changes))
    return account


def get_candidate(root, identifier):
    with connect(root) as store:
        return record(store, store.db.execute('SELECT * FROM candidates WHERE id=?', (identifier,)).fetchone())


def test_default_off_and_get_polling_never_schedule_or_pay(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    account = automation.budget(root)
    for _ in range(2):
        automation.schedule(root, account)
        automation.promote(root, account)
        response = call(app, 'GET', '/api/queue?filter=candidates').json()
        assert response['automation']['settings']['enabled'] is False
        assert not response['operations'] and not response['batches']
        assert response['candidates'][0]['state'] == row['state']


def test_auto_promotes_whole_site_and_remembers_low_grades_with_manual_restore(tmp_path):
    root = tmp_path / 'state'
    high = add_candidate(root, 'http://guild.example/eq/deep/news.html', grade=3)
    low = add_candidate(root, 'http://other.example/', grade=1)
    account = enable(root)
    assert automation.promote(root, account)['id'] == high['id']
    promoted = get_candidate(root, high['id'])
    assert promoted['state'] == 'approved_waiting_batch'
    assert promoted['scope'] == 'http://guild.example/' and promoted['scope_mode'] == 'site'
    assert promoted['decision']['origin'] == 'automatic_policy'
    assert promoted['decision']['automatic']['enrichment_budget'] == 'daily-v1'
    assert promoted['decision']['manifest_sha256'] == promoted['manifest_sha256']
    assert promoted['decision']['capture_after'] > promoted['decision']['reviewed_at']
    assert automation.promote(root, account)['id'] == low['id']
    saved = get_candidate(root, low['id'])
    assert saved['state'] == 'deferred' and saved['rating'] == low['rating'] and saved['captures'] == low['captures']
    # Lowering the configured score never discards the deliberate Saved stage.
    account.configure(settings(account, min_grade=0))
    assert automation.promote(root, account) is None
    app = create_app(root, start_worker=False)
    assert call(app, 'POST', '/api/restore', {'id': saved['id'], 'manifest_sha256': saved['manifest_sha256']}).status_code == 200
    assert automation.promote(root, account) is None
    restored = get_candidate(root, low['id'])
    assert restored['state'] == 'approval_pending'
    assert call(app, 'POST', '/api/decisions', [{'id': restored['id'], 'manifest_sha256': restored['manifest_sha256'], 'decision': 'approve'}]).status_code == 200
    assert 'automatic' not in get_candidate(root, low['id'])['decision']
    assert call(app, 'POST', '/api/undo', {'id': promoted['id'], 'manifest_sha256': promoted['manifest_sha256']}).status_code == 200
    assert automation.promote(root, account) is None
    assert get_candidate(root, high['id'])['state'] == 'approval_pending'


def test_human_scope_and_criteria_are_respected_and_bad_source_does_not_block_other_sites(tmp_path):
    root = tmp_path/'state'
    custom = add_candidate(root, 'http://one.example/guild/')
    broken = add_candidate(root, 'http://two.example/')
    focused = add_candidate(root, 'http://three.example/')
    valid = add_candidate(root, 'http://four.example/')
    app = create_app(root, start_worker=False)
    assert call(app, 'POST', '/api/scope', {'id': custom['id'], 'manifest_sha256': custom['manifest_sha256'], 'mode': 'page'}).status_code == 200
    (root / broken['captures'][0]['path']).write_text('changed source')
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET rating=json_set(rating,'$.grading_criteria','Clerics') WHERE id=?", (focused['id'],))
        store.db.commit()
    account = enable(root)
    assert automation.promote(root, account) is None  # Invalid source is held.
    assert automation.promote(root, account)['id'] == valid['id']
    assert automation.promote(root, account) is None
    assert get_candidate(root, broken['id'])['state'] == 'approval_pending'
    assert get_candidate(root, custom['id'])['scope_mode'] == 'page'
    assert automation.status(root)['held'] == 2


def test_settings_api_seeds_existing_spend_and_requires_current_revision(candidate):
    root, row = candidate
    with connect(root) as store:
        store.db.execute("INSERT INTO attempts(reserved,actual,status,created) VALUES (.4,.01,'judged',?)", (now(),))
        store.db.commit()
    app = create_app(root, start_worker=False)
    account = automation.budget(root)
    payload = settings(account, enabled=True, daily_usd=3.5, min_grade=3, grading_criteria=' Guilds ')
    response = call(app, 'POST', '/api/automation', payload)
    assert response.status_code == 200
    saved = response.json()
    assert saved['estimated_usd'] == .01 and saved['remaining_usd'] == 3.49
    assert saved['settings']['grading_criteria'] == 'Guilds'
    assert call(app, 'POST', '/api/automation', payload).status_code == 409
    assert call(app, 'POST', '/api/automation', []).status_code == 409
    assert call(app, 'POST', '/api/automation', payload, peer='203.0.113.1').status_code == 403
    assert call(create_app(root, start_worker=False), 'GET', '/api/queue').json()['automation']['settings'] == saved['settings']
    with connect(root) as store: assert not store.db.execute('SELECT 1 FROM operations').fetchone()


def test_enabling_waits_for_legacy_paid_job_without_modifying_jobs(candidate):
    root, _ = candidate
    old = {'metadata': {'name': 'untouched', 'labels': {'app': 'eqarchives-capture-import'}},
           'spec': {'template': {'spec': {'containers': [{}]}}}, 'status': {}}
    before = copy.deepcopy(old)
    class Kube:
        def jobs(self): return [old]
    account = automation.budget(root)
    with pytest.raises(CrawlError, match='Let it finish'):
        automation.configure(root, settings(account, enabled=True), Kube())
    assert old == before and not account.settings()['enabled']
    old['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
    assert automation.configure(root, settings(account, enabled=True), Kube())['settings']['enabled']


def test_continuous_runs_replenish_and_exhausted_frontier_backs_off_without_forgetting_sites(tmp_path):
    root = tmp_path/'state'
    with connect(root): pass
    account = enable(root, min_grade=3, grading_criteria='Guilds')
    automation.schedule(root, account)
    with connect(root) as store:
        operation = unpack(store.db.execute('SELECT * FROM operations').fetchone())
        assert operation['payload'] == {'automatic': True, 'fill_queue': True, 'max_candidates': 50, 'max_usd': 2,
                                        'min_grade': 3, 'grading_criteria': 'Guilds'}
        assert not store.db.execute('SELECT 1 FROM attempts').fetchone()
    automation.schedule(root, account)
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 1
        store.db.execute("UPDATE operations SET state='completed'")
        store.db.commit()
    automation.finished(root, operation, {'progress': {'stop_reason': 'links_exhausted'}})
    automation.schedule(root, account)
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 1
        store.set('automatic_activity', {'phase': 'links_exhausted', 'retry_at': '2000', 'revision': 1})
    automation.schedule(root, account)
    with connect(root) as store: assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 2


def test_budget_pause_resumes_same_operation_after_utc_reset_preserving_manual_pause(tmp_path, monkeypatch):
    root = tmp_path/'state'
    with connect(root) as store:
        manual = enqueue(store, 'discover', {'max_candidates': 1, 'max_usd': 2})
        store.db.execute("UPDATE operations SET state='interrupted',error='manual pause' WHERE id=?", (manual,))
        store.db.commit()
    account = enable(root, daily_usd=.1)
    today = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
    monkeypatch.setattr('daily_budget.instant', lambda: today)
    automation.schedule(root, account)
    account.reserve(.1)
    def run(*args): raise DailyBudgetPause(.01, '2026-10-10T00:00:00+00:00')
    monkeypatch.setattr('worker.campaign', run)
    worker = Worker(root)
    try:
        with worker.daily_budget.bind(): worker.operation(Lane.CANDIDATES)
        with connect(root) as store:
            operation = unpack(store.db.execute("SELECT * FROM operations WHERE json_extract(payload,'$.automatic')=1").fetchone())
        assert operation['state'] == 'interrupted'
        assert operation['result']['automatic_pause']['required_usd'] == .01
        automation.resume_owned(root, Lane.CANDIDATES, account)
        automation.schedule(root, account)
        assert automation.status(root)['activity']['phase'] == 'daily_budget'
        today += timedelta(minutes=2)
        automation.resume_owned(root, Lane.CANDIDATES, account)
        with connect(root) as store:
            current = unpack(store.db.execute('SELECT * FROM operations WHERE id=?', (operation['id'],)).fetchone())
            assert current['state'] == 'queued' and current['payload'] == operation['payload']
            assert store.db.execute('SELECT state FROM operations WHERE id=?', (manual,)).fetchone()[0] == 'interrupted'
        account.configure(settings(account, enabled=False))
        with worker.daily_budget.bind(): worker.operation(Lane.CANDIDATES)
        with connect(root) as store:
            assert json.loads(store.db.execute('SELECT result FROM operations WHERE id=?', (operation['id'],)).fetchone()[0])['automatic_pause']['reason'] == 'disabled'
    finally: worker.close()


def test_automatic_capture_claim_retains_undo_grace_and_marks_only_owned_work(candidate):
    root, original = candidate
    account = enable(root)
    automation.promote(root, account)
    worker = Worker(root)
    try:
        worker.capture_queue()
        with connect(root) as store: assert not store.db.execute('SELECT 1 FROM operations').fetchone()
        make_due(root)
        worker.capture_queue()
        with connect(root) as store:
            operation = unpack(store.db.execute('SELECT * FROM operations').fetchone())
            assert operation['payload']['automatic'] is True
            assert operation['payload']['sites'][0]['decision']['automatic']['min_grade'] == 2
        # Restart recovery applies only to automation-owned operations.
        with connect(root) as store:
            store.db.execute("UPDATE operations SET state='interrupted',error='Worker stopped; resume explicitly'")
            store.db.commit()
        automation.resume_owned(root, Lane.CAPTURE, account)
        with connect(root) as store: assert store.db.execute('SELECT state FROM operations').fetchone()[0] == 'queued'
    finally: worker.close()


def test_transient_auto_failures_back_off_and_unknown_failures_require_attention(candidate):
    root, _ = candidate
    account = enable(root)
    with connect(root) as store:
        identifier = enqueue(store, 'discover', {'automatic': True})
        operation = unpack(store.db.execute('SELECT * FROM operations WHERE id=?', (identifier,)).fetchone())
        pause = automation.pause_detail(CrawlError('Wayback HTTP 503'), operation)
        store.db.execute("UPDATE operations SET state='interrupted',result=?,error='Wayback HTTP 503'", (json.dumps({'automatic_pause': pause}),))
        store.db.commit()
    automation.resume_owned(root, Lane.CANDIDATES, account)
    automation.schedule(root, account)
    assert automation.status(root)['activity']['phase'] == 'retry_wait'
    with connect(root) as store:
        store.db.execute("UPDATE operations SET result='{}',error='Source identity changed'")
        store.db.commit()
    automation.schedule(root, account)
    assert automation.status(root)['activity']['phase'] == 'attention'
    assert automation.pause_detail(CrawlError('Source identity changed'), operation) is None
    assert automation.pause_detail(AutomaticStopped()) == {'reason': 'disabled'}


def test_auto_approved_site_passes_full_capture_publication_and_retirement_without_second_approval(candidate, monkeypatch):
    from captures import capture_sites
    from indexer.capture_enrichment import AUTO_POLICY
    from test_automatic_indexing import EmptyCatalog
    from automatic_indexing import prepare_next
    from import_status import write_status
    from jobs import import_name
    from test_jobs import IMAGE
    root, original = candidate
    account = enable(root)
    automation.promote(root, account)
    make_due(root)
    monkeypatch.setattr('worker.capture_sites', lambda root, batch, sites, **kwargs: capture_sites(root, batch, sites, EmptyCatalog, **kwargs))
    monkeypatch.setattr('worker.publish', lambda *args: {'commit': 'c'*40})
    monkeypatch.setenv('IMPORT_IMAGE', IMAGE)
    class Kube:
        def __init__(self): self.items = []
        def jobs(self): return self.items.copy()
        def create(self, job): self.items.append(job)
    kube = Kube()
    worker = Worker(root, kube=kube)
    try:
        worker.capture_queue()
        worker.operation(Lane.CAPTURE)
        with connect(root) as store:
            batch = unpack(store.db.execute("SELECT * FROM batches WHERE state!='capture_group'").fetchone())
        assert batch['manifest']['indexing'] == AUTO_POLICY
        assert batch['state'] == 'awaiting_review'
        assert prepare_next(root)['operation']
        worker.operation(Lane.INDEXING)
        worker.indexing()
        assert len(kube.items) == 1
        old_job = kube.items[0]
        old_job['status'] = {'conditions': [{'type': 'Failed', 'status': 'True', 'reason': 'DeadlineExceeded'}]}
        snapshot = copy.deepcopy(old_job)
        worker.indexing(); worker.indexing()
        assert len(kube.items) == 2 and old_job == snapshot
        job = kube.items[1]
        assert job['metadata']['name'] == old_job['metadata']['name'] + '-r2'
        job['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
        directory = root/'enrichment'/batch['id']; directory.mkdir(parents=True, exist_ok=True)
        write_status(directory, job['metadata']['name'], batch['manifest_sha256'], 'completed')
        worker.indexing()
        final = get_candidate(root, original['id'])
        assert final['state'] == 'indexed'
        assert final['decision']['automatic']['enrichment_budget'] == 'daily-v1'
        with connect(root) as store:
            assert store.db.execute("SELECT COUNT(*) FROM events WHERE action='site_indexing_approve'").fetchone()[0] == 0
    finally: worker.close()


def test_automatic_evidence_check_is_tagged_atomically_and_mixed_policy_batch_cannot_start(tmp_path):
    root = tmp_path/'state'
    pending = add_candidate(root, 'http://pending.example/', grade=None)
    account = enable(root)
    automation.schedule(root, account)
    with connect(root) as store:
        operation = unpack(store.db.execute('SELECT * FROM operations').fetchone())
    assert operation['kind'] == 'candidate_check' and operation['payload']['automatic'] is True
    assert operation['payload']['id'] == pending['id']
    first = add_candidate(root, 'http://automatic.example/')
    second = add_candidate(root, 'http://manual.example/', grade=2)
    automation.promote(root, account)
    app = create_app(root, start_worker=False)
    assert call(app, 'POST', '/api/decisions', [{'id':second['id'], 'manifest_sha256':second['manifest_sha256'], 'decision':'approve'}]).status_code == 200
    assert call(app, 'POST', '/api/capture', {'ids':[first['id'],second['id']]}).status_code == 409
    with connect(root) as store:
        assert not store.db.execute("SELECT 1 FROM operations WHERE kind='capture'").fetchone()
    assert get_candidate(root, first['id'])['state'] == get_candidate(root, second['id'])['state'] == 'approved_waiting_batch'


@pytest.mark.parametrize('message', ['Wayback connection failed after bounded retries', 'Wayback wall-clock budget reached', 'Cumulative Wayback budget reached; staged evidence retained'])
def test_automatic_network_and_transport_allowance_pauses_are_resumable(message):
    pause = automation.pause_detail(CrawlError(message), {'payload':{'automatic':True}})
    assert pause['reason'] == 'transient' and pause['retry_at'] > now()
