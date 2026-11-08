import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from common import CrawlError
from publication_git import PublicationError,lease,recover,run,category
from publication_recovery import register,schedule,detail,pause,summary,paused
from publisher import Publisher
from state import connect,enqueue,unpack


def failed(root,*,error='Archive Git fetch failed. Upstream details were omitted to protect credentials.',identifier='a'*32,sha='f'*64):
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'publication_requested','{}',?,NULL,NULL,NULL,'now','now')",(identifier,sha))
        op=enqueue(store,'publish',{'batch_id':identifier,'manifest_sha256':sha},commit=False)
        store.db.execute("UPDATE operations SET state='interrupted',error=? WHERE id=?",(error,op));store.db.commit()
        return op


def test_exclusive_lease_recovers_fresh_abandoned_locks_without_walking_or_losing_bytes(tmp_path):
    repo=tmp_path/'publisher.git';repo.mkdir();(repo/'shallow.lock').write_bytes(b'old checkpoint')
    first=lease(tmp_path)
    try:
        with pytest.raises(PublicationError,match='Another publisher'):lease(tmp_path)
        assert recover(tmp_path,repo)==['shallow.lock']
        assert not (repo/'shallow.lock').exists()
        assert [p.read_bytes() for p in (tmp_path/'publisher-recovery').iterdir()]==[b'old checkpoint']
        assert recover(tmp_path,repo)==[]
    finally:first.close()
    lease(tmp_path).close()


def test_active_legacy_git_process_keeps_lock_untouched(tmp_path):
    repo=tmp_path/'publisher.git';subprocess.run(['git','init','--bare','-q',str(repo)],check=True)
    lock=repo/'shallow.lock';lock.write_bytes(b'owned')
    process=subprocess.Popen(['git','--git-dir',str(repo),'hash-object','--stdin'],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
    try:
        time.sleep(.1)
        with pytest.raises(PublicationError,match='process still owns'):recover(tmp_path,repo)
        assert lock.read_bytes()==b'owned'
    finally:process.kill();process.communicate()


def test_unsafe_lock_and_recovery_paths_are_never_removed(tmp_path):
    repo=tmp_path/'publisher.git';repo.mkdir();outside=tmp_path/'other';outside.write_bytes(b'private')
    lock=repo/'shallow.lock';lock.symlink_to(outside)
    with pytest.raises(PublicationError,match='ownership'):recover(tmp_path,repo)
    assert lock.is_symlink() and outside.read_bytes()==b'private'
    lock.unlink();lock.write_bytes(b'old')
    (tmp_path/'publisher-recovery').symlink_to(tmp_path)
    with pytest.raises(PublicationError,match='recovery directory'):recover(tmp_path,repo)
    assert lock.exists()


def test_timeout_reaps_child_group_including_term_ignoring_descendants(tmp_path,monkeypatch):
    marker=tmp_path/'child'
    script="""import os,signal,time,sys
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pid=os.fork()
if pid==0:
 open(sys.argv[1],'w').write(str(os.getpid()))
while True:time.sleep(.1)
"""
    # Start the deadline only after the descendant is ready, independently of
    # coverage startup and CPU contention in the full built-image suite.
    original=subprocess.Popen
    def ready(*args,**kwargs):
        process=original(*args,**kwargs);deadline=time.monotonic()+15
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
        if not marker.exists():
            import signal
            os.killpg(process.pid,signal.SIGKILL);process.communicate()
            pytest.fail('Process group fixture never became ready')
        return process
    monkeypatch.setattr('publication_git.subprocess.Popen',ready)
    with pytest.raises(subprocess.TimeoutExpired):run([sys.executable,'-c',script,str(marker)],env=os.environ.copy(),timeout=.5)
    pid=int(marker.read_text());stat=Path('/proc')/str(pid)/'stat'
    assert not stat.exists() or stat.read_text().split()[2]=='Z'
    result=run([sys.executable,'-c','print("done")'],env=os.environ.copy())
    assert result.returncode==0 and result.stdout==b'done\n'


def test_legacy_backlog_is_requeued_once_under_the_same_approval_and_explicit_pause_wins(tmp_path):
    op=failed(tmp_path);schedule(tmp_path)
    with connect(tmp_path) as store:
        current=unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(op,)).fetchone())
        assert current['state']=='queued' and current['payload']['manifest_sha256']=='f'*64
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==1
        store.db.execute("UPDATE operations SET state='running' WHERE id=?",(op,));store.db.commit()
        register(store,current,PublicationError('Temporary Git transport problem'))
        store.db.execute("UPDATE operations SET state='interrupted' WHERE id=?",(op,));store.db.commit()
        assert detail(store,op)['attempts']==1
        pause(store,op);store.db.commit()
    schedule(tmp_path)
    with connect(tmp_path) as store:
        assert store.db.execute('SELECT state FROM operations').fetchone()[0]=='interrupted'
        assert summary(store)['attention']==1 and summary(store)['retrying']==0
        with pytest.raises(CrawlError):pause(store,op)


def test_changed_or_declined_approvals_and_unknown_failures_never_auto_publish(tmp_path):
    first=failed(tmp_path,identifier='a'*32)
    failed(tmp_path,identifier='b'*32,error='Manifest integrity failed')
    with connect(tmp_path) as store:
        store.db.execute("UPDATE batches SET state='indexing_declined' WHERE id=?",('a'*32,));store.db.commit()
    schedule(tmp_path)
    with connect(tmp_path) as store:
        assert not store.db.execute("SELECT 1 FROM operations WHERE state='queued'").fetchone()
        assert not detail(store,first)
        assert summary(store)['attention']==1


def test_one_transport_failure_backs_off_all_publication_retries_without_holding_imports(tmp_path,monkeypatch):
    first=failed(tmp_path,identifier='a'*32);second=failed(tmp_path,identifier='b'*32)
    with connect(tmp_path) as store:
        operation=unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(first,)).fetchone())
        register(store,operation,PublicationError('Git connection reset'));store.db.commit()
    schedule(tmp_path)
    assert paused(tmp_path)
    with connect(tmp_path) as store:
        assert not store.db.execute("SELECT 1 FROM operations WHERE state='queued'").fetchone()
        assert summary(store)['retrying']==2
        store.db.execute("DELETE FROM meta WHERE key='publication_backoff'");store.db.commit()
    schedule(tmp_path)
    with connect(tmp_path) as store:
        assert store.db.execute('SELECT state FROM operations WHERE id=?',(second,)).fetchone()[0]=='queued'


def test_permanent_credentials_failure_is_visible_and_not_retried_as_a_legacy_error(tmp_path):
    op=failed(tmp_path)
    with connect(tmp_path) as store:
        operation=unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(op,)).fetchone())
        register(store,operation,PublicationError('Archive Git fetch failed. SSH credential rejected','credentials',False));store.db.commit()
    schedule(tmp_path)
    with connect(tmp_path) as store:
        assert detail(store,op)['paused'] and summary(store)['attention']==1
        assert store.db.execute('SELECT state FROM operations WHERE id=?',(op,)).fetchone()[0]=='interrupted'


@pytest.mark.parametrize('text,code,retryable',[(b'shallow.lock: File exists','git_lock',True),(b'connection reset','transport',True),
    (b'Permission denied (publickey)','credentials',False),(b'No space left on device','storage',True),(b'unknown private failure','git_failure',False)])
def test_error_categories_are_typed_and_conservative(text,code,retryable):
    assert category(text)==(code,retryable)


def test_pause_and_manual_resume_api_keep_retry_intent_durable(tmp_path):
    from server import create_app
    from test_server import call
    op=failed(tmp_path)
    with connect(tmp_path) as store:
        current=unpack(store.db.execute('SELECT * FROM operations').fetchone())
        register(store,current,PublicationError('Temporary connection failure'));store.db.commit()
    app=create_app(tmp_path,start_worker=False)
    assert call(app,'POST','/api/pause-publication-retry',{'id':op}).status_code==200
    assert call(app,'POST','/api/pause-publication-retry',{}).status_code==409
    schedule(tmp_path)
    with connect(tmp_path) as store:assert detail(store,op)['paused']==1
    assert call(app,'POST','/api/resume',{'id':op}).status_code==202
    with connect(tmp_path) as store:
        assert detail(store,op)['paused']==0
        assert store.db.execute('SELECT state FROM operations WHERE id=?',(op,)).fetchone()[0]=='queued'


def test_publisher_initialization_failure_releases_lease_and_timeout_is_retryable(tmp_path,monkeypatch):
    monkeypatch.setattr('publisher.Publisher.git',lambda *_args,**_kwargs:(_ for _ in ()).throw(CrawlError('fixture init failure')))
    with pytest.raises(CrawlError):Publisher(tmp_path,remote='file:///fixture')
    lease(tmp_path).close()
    monkeypatch.undo()
    publisher=object.__new__(Publisher);publisher.repo=tmp_path/'publisher.git';publisher.reader=tmp_path/'reader.git';publisher.env={}
    def timeout(*args,**kwargs):raise subprocess.TimeoutExpired('git',600)
    monkeypatch.setattr('publisher.run_git',timeout)
    with pytest.raises(PublicationError) as caught:publisher.git('fetch','origin','master')
    assert caught.value.retryable and caught.value.code=='timeout'


def test_worker_interruption_requeues_existing_retry_without_new_operation_or_deadline(tmp_path):
    op=failed(tmp_path)
    schedule(tmp_path)
    with connect(tmp_path) as store:
        store.db.execute("UPDATE operations SET state='interrupted',error='Worker stopped; resume explicitly'");store.db.commit()
    schedule(tmp_path)
    with connect(tmp_path) as store:
        assert store.db.execute('SELECT state FROM operations WHERE id=?',(op,)).fetchone()[0]=='queued'
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==1


def test_uncertain_successful_push_is_recovered_without_duplicate_archive_commit(candidate,tmp_path):
    from conftest import manifest_for
    from common import digest
    root,row=candidate;remote=tmp_path/'remote.git'
    subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
    for key,value in [('uploadpack.allowFilter','true'),('uploadpack.allowAnySHA1InWant','true')]:
        subprocess.run(['git','--git-dir',str(remote),'config',key,value],check=True)
    import os
    env={**os.environ,'GIT_AUTHOR_NAME':'Fixture','GIT_AUTHOR_EMAIL':'fixture@example.org','GIT_COMMITTER_NAME':'Fixture','GIT_COMMITTER_EMAIL':'fixture@example.org'}
    tree=subprocess.check_output(['git','--git-dir',str(remote),'mktree'],input=b'',env=env).decode().strip()
    commit=subprocess.check_output(['git','--git-dir',str(remote),'commit-tree',tree],input=b'Initial archive\n',env=env).decode().strip()
    subprocess.run(['git','--git-dir',str(remote),'update-ref','refs/heads/master',commit],check=True)
    publisher=Publisher(root,remote='file://'+str(remote));real=publisher.git;pushes=[]
    def ambiguous(*args,**kwargs):
        result=real(*args,**kwargs)
        if args[0]=='push':pushes.append(args);return None
        return result
    publisher.git=ambiguous
    try:
        manifest=manifest_for(row);result=publisher.publish(manifest,digest(manifest))
        assert result['recovered'] is True and len(pushes)==1
        assert subprocess.check_output(['git','--git-dir',str(remote),'rev-list','--count','master']).strip()==b'2'
    finally:publisher.close()
