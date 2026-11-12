import copy
from datetime import datetime, timedelta, timezone
import json

import pytest

import automation
from common import CrawlError, digest
from daily_budget import DailyBudgetPause, current_budget
from import_status import read_budget_pause, status_path, write_status
from jobs import import_job, import_name
from server import create_app
from state import connect, unpack
from test_automatic_indexing import completed, published
from test_daily_budget import settings
from test_jobs import IMAGE
from test_server import call
from worker import Worker


def test_budget_wait_is_not_completion_and_resumes_new_job_without_changing_reindex(tmp_path, monkeypatch):
    root = tmp_path/'state'
    row, manifest = completed(root)
    immutable = published(root, manifest, 'indexing')
    with connect(root) as store: batch = unpack(store.db.execute('SELECT * FROM batches').fetchone())
    old = import_job(batch, IMAGE)
    old['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
    old['metadata']['uid'] = 'untouched-old-job'
    original = copy.deepcopy(old)
    reindex = {'metadata': {'name': 'full-reindex', 'uid': 'untouched-reindex'}, 'spec': {}, 'status': {'active': 1}}
    class Kube:
        def __init__(self): self.items = [old, reindex]; self.created = []
        def jobs(self): return copy.deepcopy(self.items)
        def create(self, value): self.created.append(value); self.items.append(value)
    account = automation.budget(root)
    account.configure(settings(account, enabled=True, daily_usd=.1))
    today = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
    monkeypatch.setattr('daily_budget.instant', lambda: today)
    account.reserve(.1)
    directory = root/'enrichment'/batch['id']; directory.mkdir(parents=True)
    write_status(directory, import_name(batch), batch['manifest_sha256'], 'waiting_budget', DailyBudgetPause(.01, '2026-10-10T00:00:00+00:00'))
    kube = Kube()
    monkeypatch.setenv('IMPORT_IMAGE', IMAGE)
    worker = Worker(root, kube=kube)
    try:
        worker.indexing(); worker.indexing()
        app = create_app(root, start_worker=False)
        view = call(app, 'GET', '/api/candidate?id='+row['id']).json()
        assert view['candidate']['stage'] == 'indexing'
        assert view['review']['state'] == 'index_budget_waiting'
        assert view['review']['job']['required_usd'] == .01
        assert not kube.created
        today += timedelta(minutes=2)
        worker.indexing()
        with connect(root) as store:
            resumed = unpack(store.db.execute('SELECT * FROM batches').fetchone())
        assert resumed['state'] == 'published_waiting_index' and resumed['job']['attempt'] == 2
        worker.indexing()
        assert len(kube.created)==1  # Foreground enrichment no longer waits for the full reindex.
        assert old == original and reindex['status']['active'] == 1
        worker.indexing(); worker.indexing()
        assert len(kube.created) == 1
        new = kube.created[0]
        assert new['metadata']['name'] == old['metadata']['name']+'-r2'
        assert new['spec']['template']['spec']['containers'][0]['command'] == old['spec']['template']['spec']['containers'][0]['command']
        # The previous wait diagnostic must not override the current attempt.
        new['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
        write_status(directory, new['metadata']['name'], batch['manifest_sha256'], 'completed')
        worker.indexing()
        assert call(app, 'GET', '/api/candidate?id='+row['id']).json()['candidate']['stage'] == 'history'
        assert (root/'batches'/batch['id']/'approved.json').read_bytes() == immutable
        assert old == original
    finally: worker.close()


def test_indexer_budget_exit_retains_manifest_bound_wait_and_closes_context(candidate, tmp_path, monkeypatch):
    from conftest import manifest_for
    import index_captures
    root, row = candidate
    manifest = manifest_for(row)
    batch = {'manifest': manifest, 'manifest_sha256': digest(manifest), 'publication': {'commit': 'c'*40}}
    account = automation.budget(root)
    account.configure(settings(account, enabled=True, daily_usd=.1))
    account.reserve(.1)
    job = 'eqarchives-captures-'+manifest['batch_id']
    monkeypatch.setenv('IMPORT_JOB_NAME', job)
    monkeypatch.setenv('LUNA_DAILY_BUDGET_ROOT', str(account.root))
    monkeypatch.setattr('sys.argv', ['index_captures', '--root', str(root), '--batch', 'approved.json',
        '--manifest-sha256', batch['manifest_sha256'], '--enrichment-root', str(root/'enrichment')])
    monkeypatch.setattr(index_captures, 'read_batch', lambda *args: batch)
    monkeypatch.setattr(index_captures, 'Services', lambda **kwargs: kwargs['enricher'])
    def spend(*args): current_budget.get().reserve(.01)
    monkeypatch.setattr(index_captures, 'index_batch', spend)
    assert index_captures.main() == 0
    waiting = read_budget_pause(root, {'id': manifest['batch_id'], 'manifest_sha256': batch['manifest_sha256']}, job)
    assert waiting['required_usd'] == .01
    assert current_budget.get() is None
    assert account.snapshot()['unresolved_usd'] == .1


@pytest.mark.parametrize('change', [{'required_usd': -1}, {'required_usd': 'secret'}, {'required_usd': True},
    {'retry_at': 'invalid'}, {'retry_at': '2026-10-10'}, {'manifest_sha256': 'stale'}, {'state': 'completed'}])
def test_budget_diagnostics_reject_invalid_or_stale_content(tmp_path, change):
    batch = {'id': 'a'*32, 'manifest_sha256': 'b'*64}
    directory = tmp_path/'enrichment'/batch['id']; directory.mkdir(parents=True)
    job = import_name(batch)
    write_status(directory, job, batch['manifest_sha256'], 'waiting_budget', DailyBudgetPause(.01, '2026-10-10T00:00:00+00:00'))
    path = status_path(directory, job)
    value = json.loads(path.read_text()); path.write_text(json.dumps({**value, **change}))
    assert read_budget_pause(tmp_path, batch, job) is None


@pytest.mark.parametrize('memory',['512Mi','2Gi'])
def test_oom_only_migrates_the_undersized_import_profile_and_preserves_old_job(tmp_path,monkeypatch,memory):
    root=tmp_path/'state';row,manifest=completed(root);immutable=published(root,manifest,'indexing')
    with connect(root) as store:batch=unpack(store.db.execute('SELECT * FROM batches').fetchone())
    old=import_job(batch,IMAGE);old['spec']['template']['spec']['containers'][0]['resources']['limits']['memory']=memory
    old['status']={'conditions':[{'type':'Failed','status':'True'}]};original=copy.deepcopy(old)
    class Kube:
        def __init__(self):self.items=[old];self.created=[]
        def jobs(self):return copy.deepcopy(self.items)
        def create(self,value):self.created.append(value);self.items.append(value)
        def job_failure(self,_):return 'Indexer exceeded its memory limit (OOMKilled); saved sources and paid results are retained.'
    kube=Kube();monkeypatch.setenv('IMPORT_IMAGE',IMAGE);worker=Worker(root,kube=kube)
    try:
        worker.indexing()
        with connect(root) as store:current=unpack(store.db.execute('SELECT * FROM batches').fetchone())
        if memory=='512Mi':
            assert current['state']=='published_waiting_index' and current['job']['attempt']==2
            worker.indexing();assert len(kube.created)==1
            assert kube.created[0]['spec']['template']['spec']['containers'][0]['resources']['limits']['memory']=='2Gi'
        else:
            assert current['state']=='index_failed' and 'OOMKilled' in current['error'] and not kube.created
        assert old==original and (root/'batches'/batch['id']/'approved.json').read_bytes()==immutable
    finally:worker.close()


@pytest.mark.parametrize('shared_ledger', [False, True])
def test_manual_deadline_continues_across_worker_restarts_without_renewing_budget(tmp_path, monkeypatch, shared_ledger):
    root = tmp_path/'state'
    row, manifest = completed(root)
    immutable = published(root, manifest, 'indexing')
    with connect(root) as store:
        batch = unpack(store.db.execute('SELECT * FROM batches').fetchone())
        approval = store.db.execute('SELECT decision FROM candidates WHERE id=?', (row['id'],)).fetchone()[0]
    old = import_job(batch, IMAGE)
    if not shared_ledger:
        container = old['spec']['template']['spec']['containers'][0]
        container['env'] = [item for item in container['env'] if item['name'] != 'LUNA_DAILY_BUDGET_ROOT']
    old['status'] = {'conditions': [{'type': 'Failed', 'status': 'True', 'reason': 'DeadlineExceeded'}]}
    old['metadata']['uid'] = 'retained-timeout'
    original = copy.deepcopy(old)
    reindex = {'metadata': {'name': 'full-reindex', 'uid': 'retained-reindex'}, 'spec': {}, 'status': {'active': 1}}
    reindex_original = copy.deepcopy(reindex)
    directory = root/'enrichment'/batch['id']; directory.mkdir(parents=True)
    retained = directory/'retained-paid-result.json'; retained.write_text('{"paid_result":"retained"}')
    class Kube:
        def __init__(self): self.items = [old, reindex]; self.created = []
        def jobs(self): return copy.deepcopy(self.items)
        def create(self, value): self.created.append(value); self.items.append(value)
    kube = Kube(); monkeypatch.setenv('IMPORT_IMAGE', IMAGE)
    for attempt in (2, 3):
        worker = Worker(root, kube=kube)
        try:
            worker.indexing()
            with connect(root) as store:
                queued = unpack(store.db.execute('SELECT * FROM batches').fetchone())
            assert queued['state'] == 'published_waiting_index'
            assert queued['job']['attempt'] == attempt
            assert queued['manifest']['indexing'] == {'enrichment': True, 'model': 'gpt-6-luna', 'max_enrichment_usd': 2}
            worker.indexing(); worker.indexing()
            assert len(kube.created) == attempt-1
            new = kube.created[-1]
            assert new['metadata']['name'] == old['metadata']['name'] + f'-r{attempt}'
            assert new['spec']['template']['spec']['containers'][0]['command'] == old['spec']['template']['spec']['containers'][0]['command']
            assert new['metadata']['annotations']['eqarchives.org/manifest-sha256'] == batch['manifest_sha256']
            assert (root/'batches'/batch['id']/'approved.json').read_bytes() == immutable
            assert retained.read_text() == '{"paid_result":"retained"}'
            if attempt == 2:
                new['status'] = {'conditions': [{'type': 'Failed', 'status': 'True', 'reason': 'DeadlineExceeded'}]}
            else:
                new['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
                write_status(directory, new['metadata']['name'], batch['manifest_sha256'], 'completed')
                worker.indexing()
                with connect(root) as store:
                    assert store.db.execute('SELECT state FROM batches').fetchone()[0] == 'indexed'
                    assert store.db.execute('SELECT decision FROM candidates WHERE id=?', (row['id'],)).fetchone()[0] == approval
        finally: worker.close()
    assert old == original and reindex == reindex_original


@pytest.mark.parametrize('failure', ['diagnostic', 'source', 'publication', 'different_failure'])
def test_timeout_recovery_does_not_override_saved_failures_or_changed_sources(tmp_path, monkeypatch, failure):
    from import_status import EnrichmentError
    root = tmp_path/'state'
    row, manifest = completed(root)
    published(root, manifest, 'indexing')
    with connect(root) as store:
        batch = unpack(store.db.execute('SELECT * FROM batches').fetchone())
    old = import_job(batch, IMAGE)
    old['status'] = {'conditions': [{'type': 'Failed', 'status': 'True',
        'reason': 'BackoffLimitExceeded' if failure == 'different_failure' else 'DeadlineExceeded'}]}
    original = copy.deepcopy(old)
    if failure == 'diagnostic':
        directory = root/'enrichment'/batch['id']; directory.mkdir(parents=True)
        write_status(directory, import_name(batch), batch['manifest_sha256'], 'failed', EnrichmentError('budget'))
    elif failure == 'source':
        (root/manifest['captures'][0]['path']).write_bytes(b'changed source')
    elif failure == 'publication':
        path = root/'batches'/batch['id']/'approved.json'
        value = json.loads(path.read_text()); value['publication']['commit'] = 'd'*40
        path.write_text(json.dumps(value))
    class Kube:
        def jobs(self): return [copy.deepcopy(old)]
        def create(self, value): pytest.fail('A genuine failure must not start another Job')
    monkeypatch.setenv('IMPORT_IMAGE', IMAGE)
    worker = Worker(root, kube=Kube())
    try:
        worker.indexing(); worker.indexing()
        with connect(root) as store:
            current = unpack(store.db.execute('SELECT * FROM batches').fetchone())
        assert current['state'] == 'index_failed' and current['job']['attempt'] == 1
        assert old == original
    finally: worker.close()
