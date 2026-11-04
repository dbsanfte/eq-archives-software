"""Real SQLite contention must not kill a worker or replay external work."""
from contextlib import contextmanager
import json
import sqlite3
import threading

import pytest

from common import CrawlError,digest
from conftest import manifest_for
from state import Lane, connect, database_busy, enqueue
from worker import Worker


@pytest.mark.parametrize('lane',list(Lane))
def test_worker_survives_a_real_claim_lock_and_claims_once_after_release(candidate,monkeypatch,lane):
    root,_=candidate
    worker=Worker(root)
    with connect(root) as store:
        operation=enqueue(store,{Lane.CANDIDATES:'discover',Lane.CAPTURE:'capture',Lane.INDEXING:'publish'}[lane],{})
    real_connect=connect
    @contextmanager
    def immediate(root):
        with real_connect(root) as store:
            store.db.execute('PRAGMA busy_timeout=0')
            yield store
    monkeypatch.setattr('worker.connect',immediate)
    monkeypatch.setattr('worker.automation.resume_owned',lambda *a:None)
    monkeypatch.setattr('worker.automation.promote',lambda *a:None)
    monkeypatch.setattr('worker.automation.schedule',lambda *a:None)
    monkeypatch.setattr('worker.prepare_next',lambda *a:None)
    monkeypatch.setattr(worker,'capture_queue',lambda:None)
    monkeypatch.setattr(worker,'indexing',lambda:None)
    # A failed preclaim must leave the operation queued; avoid invoking a real
    # downloader/publisher after release by observing the claim at BEGIN.
    claimed=threading.Event()
    def claim_once(current):
        with immediate(root) as store:
            store.db.execute('BEGIN IMMEDIATE')
            store.db.execute("UPDATE operations SET state='completed' WHERE id=? AND state='queued'",(operation,))
            store.db.commit()
        claimed.set();worker.stop.set()
    monkeypatch.setattr(worker,'operation',claim_once)
    # Speed up only the idle wait, retaining real SQLite lock behaviour.
    original_wait=worker.stop.wait
    monkeypatch.setattr(worker.stop,'wait',lambda _:original_wait(.01))
    errors=[]
    def run():
        try:worker.run_lane(lane)
        except Exception as error:errors.append(error)
    with real_connect(root) as holder:
        holder.db.execute('BEGIN IMMEDIATE')
        thread=threading.Thread(target=run);thread.start()
        original_wait(.05)
        assert thread.is_alive(),errors
        assert not claimed.is_set()
        assert holder.db.execute('SELECT state FROM operations WHERE id=?',(operation,)).fetchone()[0]=='queued'
        holder.db.rollback()
    try:
        assert claimed.wait(3),errors
        thread.join(3)
        assert not errors
        with real_connect(root) as store:
            assert store.db.execute('SELECT state FROM operations WHERE id=?',(operation,)).fetchone()[0]=='completed'
    finally:
        worker.stop.set();thread.join(3);worker.close()


def test_non_contention_database_errors_remain_visible(candidate,monkeypatch):
    root,_=candidate;worker=Worker(root)
    monkeypatch.setattr('worker.automation.resume_owned',lambda *a:None)
    monkeypatch.setattr('worker.prepare_next',lambda *a:None)
    monkeypatch.setattr(worker,'operation',lambda _:(_ for _ in ()).throw(sqlite3.OperationalError('no such table: operations')))
    try:
        with pytest.raises(sqlite3.OperationalError,match='no such table'):
            worker.run_lane(Lane.INDEXING)
    finally:worker.close()


@pytest.mark.parametrize('kind',['capture','publish'])
@pytest.mark.parametrize('fails',[False,True])
def test_result_write_lock_does_not_replay_capture_or_publication(candidate,monkeypatch,kind,fails):
    root,row=candidate;worker=Worker(root);manifest=manifest_for(row)
    with connect(root) as store:
        state='capturing' if kind=='capture' else 'publication_requested'
        store.db.execute('INSERT INTO batches VALUES (?,?,?,?,NULL,NULL,NULL,?,?)',
                         (manifest['batch_id'],state,json.dumps(manifest),digest(manifest),'now','now'))
        store.db.execute('UPDATE candidates SET state=?',('capturing' if kind=='capture' else 'approved_waiting_publication',))
        store.db.commit()
        operation=enqueue(store,kind,{'batch_id':manifest['batch_id'],'sites':manifest['sites'],'manifest_sha256':digest(manifest)})
    @contextmanager
    def immediate(root):
        with connect(root) as store:
            store.db.execute('PRAGMA busy_timeout=0');yield store
    monkeypatch.setattr('worker.connect',immediate)
    locked=threading.Event();release=threading.Event();waiting=threading.Event();calls=[];errors=[]
    def holder():
        with connect(root) as store:
            store.db.execute('BEGIN IMMEDIATE');locked.set()
            assert release.wait(5)
            store.db.rollback()
    lock_thread=threading.Thread(target=holder)
    def external(*a,**k):
        calls.append(kind);lock_thread.start();assert locked.wait(3)
        if fails:raise CrawlError('Fixture acquisition/publication paused')
        return manifest if kind=='capture' else {'commit':'b'*40}
    monkeypatch.setattr('worker.capture_sites' if kind=='capture' else 'worker.publish',external)
    monkeypatch.setattr('worker.LOG.warning',lambda *a:waiting.set())
    original_wait=worker.stop.wait
    monkeypatch.setattr(worker.stop,'wait',lambda _:original_wait(.01))
    def run():
        try:worker.operation(Lane.CAPTURE if kind=='capture' else Lane.INDEXING)
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=run)
    try:
        thread.start();assert waiting.wait(3),errors
        with connect(root) as store:
            assert store.db.execute('SELECT state FROM operations WHERE id=?',(operation,)).fetchone()[0]=='running'
        assert calls==[kind] and thread.is_alive()
        release.set();thread.join(5)
        assert not errors and not thread.is_alive() and calls==[kind]
        with connect(root) as store:
            saved=store.db.execute('SELECT state,error FROM operations WHERE id=?',(operation,)).fetchone()
            assert saved['state']==('interrupted' if fails else 'completed')
            if fails:assert saved['error']=='Fixture acquisition/publication paused'
            assert store.db.execute('SELECT state FROM batches').fetchone()[0]==(state if fails else 'awaiting_review' if kind=='capture' else 'published_waiting_index')
    finally:
        release.set();thread.join(5)
        if lock_thread.ident:lock_thread.join(5)
        worker.close()


def test_metadata_retry_stops_on_shutdown_and_does_not_hide_storage_damage(candidate):
    root,_=candidate;worker=Worker(root)
    def fail(message):raise sqlite3.OperationalError(message)
    try:
        with pytest.raises(sqlite3.OperationalError,match='disk I/O'):
            worker.save_state(fail,'disk I/O error')
        worker.stop.set()
        with pytest.raises(sqlite3.OperationalError,match='locked'):
            worker.save_state(fail,'database is locked')
    finally:worker.close()


@pytest.mark.parametrize('code,expected',[(sqlite3.SQLITE_BUSY,True),(sqlite3.SQLITE_BUSY_SNAPSHOT,True),
    (sqlite3.SQLITE_LOCKED,True),(sqlite3.SQLITE_LOCKED_SHAREDCACHE,True),(sqlite3.SQLITE_CORRUPT,False)])
def test_only_sqlite_contention_codes_are_retryable(code,expected):
    error=sqlite3.OperationalError('deliberately untranslated error')
    error.sqlite_errorcode=code
    assert database_busy(error) is expected
    assert not database_busy(CrawlError('database is locked'))


def test_preflight_contention_keeps_original_approval_ready_for_automatic_retry(tmp_path,monkeypatch):
    from test_automatic_indexing import completed
    from automatic_indexing import prepare_next
    root=tmp_path/'state';row,manifest=completed(root)
    monkeypatch.setattr('automatic_indexing.check_manifest',lambda *a:(_ for _ in ()).throw(sqlite3.OperationalError('database is locked')))
    with pytest.raises(sqlite3.OperationalError,match='locked'):prepare_next(root)
    with connect(root) as store:
        assert store.db.execute('SELECT state,error FROM batches').fetchone()[:]==('awaiting_review',None)
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()


def test_all_worker_threads_and_http_health_survive_contention(candidate,monkeypatch):
    from server import create_app
    from test_server import call
    root,_=candidate;app=create_app(root,start_worker=True);worker=Worker(root);app.state.worker=worker
    monkeypatch.setattr('worker.automation.resume_owned',lambda *a:None)
    monkeypatch.setattr('worker.automation.promote',lambda *a:None)
    monkeypatch.setattr('worker.automation.schedule',lambda *a:None)
    monkeypatch.setattr('worker.prepare_next',lambda *a:None)
    monkeypatch.setattr(worker,'capture_queue',lambda:None)
    monkeypatch.setattr(worker,'indexing',lambda:None)
    @contextmanager
    def immediate(root):
        with connect(root) as store:
            store.db.execute('PRAGMA busy_timeout=0');yield store
    monkeypatch.setattr('worker.connect',immediate)
    failed={lane:threading.Event() for lane in Lane};recovered={lane:threading.Event() for lane in Lane}
    original=worker.operation
    def attempt(lane):
        try:original(lane)
        except sqlite3.OperationalError:
            failed[lane].set();raise
        recovered[lane].set()
    monkeypatch.setattr(worker,'operation',attempt)
    original_wait=worker.stop.wait
    monkeypatch.setattr(worker.stop,'wait',lambda _:original_wait(.01))
    try:
        with connect(root) as holder:
            holder.db.execute('BEGIN IMMEDIATE');worker.start()
            assert all(event.wait(3) for event in failed.values())
            assert call(app,'GET','/healthz').status_code==200
            assert worker.healthy()
            holder.db.rollback()
        assert all(event.wait(3) for event in recovered.values())
        assert call(app,'GET','/healthz').status_code==200
    finally:worker.close()


def test_created_import_is_reconciled_after_a_metadata_lock_without_resubmission(candidate,monkeypatch):
    root,row=candidate;manifest=manifest_for(row)
    monkeypatch.setenv('IMPORT_IMAGE','dbsanfte/frontend@sha256:'+'1'*64)
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'published_waiting_index',?,?,?,NULL,NULL,'now','now')",
                         (manifest['batch_id'],json.dumps(manifest),digest(manifest),json.dumps({'commit':'c'*40})))
        store.db.execute("UPDATE candidates SET state='published'");store.db.commit()
    @contextmanager
    def immediate(root):
        with connect(root) as store:
            store.db.execute('PRAGMA busy_timeout=0');yield store
    monkeypatch.setattr('worker.connect',immediate)
    locked=threading.Event();release=threading.Event()
    def holder():
        with connect(root) as store:
            store.db.execute('BEGIN IMMEDIATE');locked.set()
            assert release.wait(5)
            store.db.rollback()
    lock_thread=threading.Thread(target=holder)
    class Kube:
        def __init__(self):self.items=[];self.calls=0
        def jobs(self):return self.items.copy()
        def create(self,job):
            self.calls+=1;self.items.append(job);lock_thread.start();assert locked.wait(3)
    kube=Kube();worker=Worker(root,kube=kube)
    try:
        with pytest.raises(sqlite3.OperationalError,match='locked'):worker.indexing()
        release.set();lock_thread.join(3)
        with connect(root) as store:
            assert store.db.execute('SELECT state,error FROM batches').fetchone()[:]==('published_waiting_index',None)
        worker.indexing()
        assert kube.calls==1
        with connect(root) as store:
            assert store.db.execute('SELECT state FROM batches').fetchone()[0]=='indexing'
    finally:
        release.set()
        if lock_thread.ident:lock_thread.join(3)
        worker.close()
