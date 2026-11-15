"""A bad supporting URL or a metadata lock cannot strand a whole approved site."""
import json
import sqlite3
from pathlib import Path

import pytest

from captures import capture_sites,check_manifest
from common import CrawlError,digest
from conftest import manifest_for


@pytest.mark.parametrize('bad_url',[
    'http://guild.example/eq/bad%00name.png',
    'http://guild.example/eq/search.php?name=%81',
])
def test_unrepresentable_file_is_retained_as_a_gap_and_later_files_finish(candidate,bad_url):
    root,row=candidate;site={**manifest_for(row)['sites'][0],'capture_policy':'complete-files-v1'}
    class Downloader:
        def __init__(self,*args):pass
        def close(self):pass
        def call(self,job):
            if job['op']=='scope_list':
                return {'captures':[{'url':u,'timestamp':'20000101000000'} for u in (bad_url,site['scope']+'good.png')]}
            data=b'preserved exact source';Path(job['destination']).write_bytes(data)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(data),'bytes':len(data),'content_type':'image/png'}
    result=capture_sites(root,'b'*32,[site],Downloader)
    assert len(result['captures'])==2
    assert result['capture_coverage'][site['id']]['state']=='complete_with_gaps'
    note=next(n for n in result['notes'] if n['url']==bad_url)
    assert note['kind']=='archive_path_unavailable'
    assert (root/note['retained_source']['path']).read_bytes()==b'preserved exact source'
    check_manifest(root,result)


def test_colliding_url_spellings_retain_conflicting_bytes_privately(candidate):
    root,row=candidate;site={**manifest_for(row)['sites'][0],'capture_policy':'complete-files-v1'}
    urls=[site['scope']+'one+two.png',site['scope']+'one%20two.png',site['scope']+'one two.png']
    # The first two URL spellings map to one downloader filename. The second
    # body differs and must remain private, while later ordinary files proceed.
    class Downloader:
        def __init__(self,*args):pass
        def close(self):pass
        def call(self,job):
            if job['op']=='scope_list':
                return {'captures':[{'url':u,'timestamp':'20000101000000'} for u in urls[:2]+[site['scope']+'good.png']]}
            data=job['url'].encode();Path(job['destination']).write_bytes(data)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(data),'bytes':len(data),'content_type':'image/png'}
    result=capture_sites(root,'c'*32,[site],Downloader)
    assert len(result['captures'])==3
    note=next(n for n in result['notes'] if n.get('kind')=='archive_path_collision')
    assert note['url']==urls[1]
    assert (root/note['retained_source']['path']).read_bytes()==urls[1].encode()
    assert result['capture_retry']['files']==1
    check_manifest(root,result)


def test_invalid_supporting_catalog_is_a_gap_not_a_site_failure(candidate):
    root,row=candidate;site={**manifest_for(row)['sites'][0],'capture_policy':'complete-files-v1'}
    with sqlite3.connect(root/'crawl.sqlite3') as db:
        capture=json.loads(db.execute('SELECT captures FROM candidates').fetchone()[0])[0]
    source=root/capture['path'];source.write_bytes(b'<img src="http://assets.example/file[1].jpg">EverQuest')
    capture.update(bytes=source.stat().st_size,sha256=digest(source.read_bytes()))
    site['captures']=[capture]
    class Downloader:
        def __init__(self,*args):pass
        def close(self):pass
        def call(self,job):
            if job['url'].startswith('http://assets.example/'):
                raise CrawlError('Invalid approved capture inventory scope')
            return {'captures':[]}
    result=capture_sites(root,'d'*32,[site],Downloader)
    assert result['capture_retry']['lookups']==1
    assert result['capture_coverage'][site['id']]['state']=='complete_with_gaps'


def test_metadata_transaction_retries_a_real_stale_wal_snapshot(tmp_path):
    from state import retry_metadata
    p=tmp_path/'state.sqlite';one=sqlite3.connect(p);one.execute('PRAGMA journal_mode=WAL')
    one.execute('CREATE TABLE item(value)');one.execute('INSERT INTO item VALUES (1)');one.commit();one.close()
    attempts=[]
    @retry_metadata
    def update():
        db=sqlite3.connect(p,timeout=0)
        try:
            db.execute('BEGIN');value=db.execute('SELECT value FROM item').fetchone()[0]
            attempts.append(value)
            if len(attempts)==1:
                with sqlite3.connect(p) as writer:writer.execute('UPDATE item SET value=2')
            db.execute('UPDATE item SET value=?',(value+10,));db.commit()
        finally:db.close()
    update()
    assert attempts==[1,2]
    with sqlite3.connect(p) as db:assert db.execute('SELECT value FROM item').fetchone()[0]==12


@pytest.mark.parametrize('code,category,retryable',[
    (sqlite3.SQLITE_BUSY,'storage_busy',True),
    (sqlite3.SQLITE_FULL,'storage_full',False),
    (sqlite3.SQLITE_READONLY,'storage_readonly',False),
    (sqlite3.SQLITE_IOERR,'storage_io',False),
    (sqlite3.SQLITE_CORRUPT,'storage_corrupt',False),
])
def test_storage_diagnostics_are_typed_and_do_not_leak_sql(candidate,code,category,retryable):
    from state import Lane,connect,enqueue,unpack
    from worker import Worker
    from storage_recovery import schedule
    root,_=candidate;worker=Worker(root)
    try:
        with connect(root) as store:
            identifier=enqueue(store,'discover',{'max_usd':2,'min_grade':2,'fill_queue':True,'max_candidates':50})
            op=unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(identifier,)).fetchone())
        error=sqlite3.OperationalError('private SQL and database path');error.sqlite_errorcode=code
        worker.operation_interrupted(op,error)
        with connect(root) as store:
            saved=unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(identifier,)).fetchone())
            assert 'private SQL' not in saved['error']
            assert saved['result']['failure']=={'category':category,'sqlite_code':code,'retryable':retryable}
            retries=store.db.execute('SELECT count(*) FROM operation_storage_retries').fetchone()[0]
            assert retries==int(retryable)
            store.db.execute("UPDATE operation_storage_retries SET retry_at='2000-01-01'");store.db.commit()
        schedule(root,Lane.CANDIDATES)
        with connect(root) as store:
            saved=unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(identifier,)).fetchone())
            assert saved['state']==('queued' if retryable else 'interrupted')
            assert saved['payload']==op['payload']
    finally:worker.close()


@pytest.mark.parametrize('error',[
    'Operation failed (OperationalError); staged evidence retained',
    'Invalid approved capture inventory scope',
    'URL has an invalid encoded archive filename',
    'Exact sources collide in the archive filename convention; capture paused without overwriting files',
])
def test_legacy_capture_failures_receive_one_recovery_without_losing_approval(candidate,monkeypatch,error):
    import capture_recovery
    from test_capture_resume import interrupted
    from server import create_app
    from state import connect
    root,row=candidate;app=create_app(root,start_worker=False)
    operation,payload,progress=interrupted(root,app,row)
    with connect(root) as store:
        store.db.execute('UPDATE operations SET error=? WHERE id=?',(error,operation));store.db.commit()
    monkeypatch.setattr(capture_recovery,'now',lambda:'2026-11-15T00:00:00+00:00')
    capture_recovery.schedule(root)
    with connect(root) as store:
        assert store.db.execute('SELECT count(*) FROM capture_retries WHERE operation=?',(operation,)).fetchone()[0]==1
    monkeypatch.setattr(capture_recovery,'now',lambda:'2026-11-15T00:06:00+00:00')
    capture_recovery.schedule(root)
    with connect(root) as store:
        saved=store.db.execute('SELECT state,payload,result FROM operations WHERE id=?',(operation,)).fetchone()
        assert saved['state']=='resume_queued'
        assert json.loads(saved['payload'])==payload
        assert json.loads(saved['result'])==progress


@pytest.mark.parametrize('reverse',[False,True])
def test_file_directory_conflicts_are_retained_without_blocking_publication(candidate,reverse):
    root,row=candidate;site={**manifest_for(row)['sites'][0],'capture_policy':'complete-files-v1'}
    urls=[site['scope']+'robots.txt',site['scope']+'robots.txt/ad.gif']
    if reverse:urls.reverse()
    class Downloader:
        def __init__(self,*args):pass
        def close(self):pass
        def call(self,job):
            if job['op']=='scope_list':return {'captures':[{'url':u,'timestamp':'20000101000000'} for u in urls]}
            data=b'saved';Path(job['destination']).write_bytes(data)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(data),'bytes':len(data),'content_type':'image/png'}
    result=capture_sites(root,'e'*32,[site],Downloader)
    assert len(result['captures'])==2
    note=next(n for n in result['notes'] if n.get('kind')=='archive_path_collision')
    assert note['url']==urls[1]
    assert (root/note['retained_source']['path']).read_bytes()==b'saved'
    check_manifest(root,result)


def test_storage_retry_backoff_reuses_one_ledger_and_never_retries_nonbusy_errors(candidate,monkeypatch):
    from state import connect,retry_metadata
    from storage_recovery import diagnostic,register,schedule
    from state import Lane
    root,_=candidate
    assert diagnostic(ValueError('source issue')) is None
    error=sqlite3.OperationalError('busy');error.sqlite_errorcode=sqlite3.SQLITE_LOCKED
    with connect(root) as store:
        operation={'id':'f'*32,'kind':'discover'}
        register(store,operation,diagnostic(error));register(store,operation,diagnostic(error));store.db.commit()
        assert store.db.execute('SELECT attempts FROM operation_storage_retries').fetchone()[0]==2
        assert register(store,{'id':'e'*32,'kind':'capture'},diagnostic(error)) is None
    schedule(root,Lane.CAPTURE)
    calls=[];monkeypatch.setattr('state.time.sleep',lambda _:None)
    @retry_metadata
    def busy():
        calls.append(1);raise error
    with pytest.raises(sqlite3.OperationalError):busy()
    assert len(calls)==4
    @retry_metadata
    def bad_storage():
        calls.append(2);e=sqlite3.OperationalError('I/O');e.sqlite_errorcode=sqlite3.SQLITE_IOERR;raise e
    with pytest.raises(sqlite3.OperationalError):bad_storage()
    assert calls==[1,1,1,1,2]


def test_explicitly_cancelled_legacy_capture_is_not_reactivated(candidate):
    from capture_recovery import schedule
    from test_capture_resume import interrupted
    from state import connect,unpack
    from capture_queue import queue_resume,cancel_resume
    from server import create_app
    root,row=candidate;operation,_,_=interrupted(root,create_app(root,start_worker=False),row)
    with connect(root) as store:
        store.db.execute("UPDATE operations SET error='Operation failed (OperationalError); staged evidence retained' WHERE id=?",(operation,))
        op=unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(operation,)).fetchone())
        queue_resume(store,op);cancel_resume(store,op);store.db.commit()
    schedule(root)
    with connect(root) as store:
        assert store.db.execute('SELECT state FROM operations WHERE id=?',(operation,)).fetchone()[0]=='interrupted'
        assert store.db.execute('SELECT count(*) FROM capture_retries WHERE operation=?',(operation,)).fetchone()[0]==0


def test_old_storage_retry_receipt_cannot_block_a_later_recoverable_operation(candidate):
    from state import Lane,connect,enqueue
    from storage_recovery import schedule
    root,_=candidate
    with connect(root) as store:
        first=enqueue(store,'discover',{'max_usd':2})
        store.db.execute("UPDATE operations SET state='interrupted' WHERE id=?",(first,));store.db.commit()
        second=enqueue(store,'candidate_check',{'id':'a'*24})
        for op,result in ((first,{}),(second,{'failure':{'category':'storage_busy','retryable':True}})):
            store.db.execute("UPDATE operations SET state='interrupted',result=? WHERE id=?",(json.dumps(result),op))
            store.db.execute("INSERT INTO operation_storage_retries VALUES (?,1,'2000-01-01')",(op,))
        store.db.commit()
    schedule(root,Lane.CANDIDATES)
    with connect(root) as store:
        states=dict(store.db.execute('SELECT id,state FROM operations'))
        assert states[first]=='interrupted' and states[second]=='queued'


def test_legacy_recovery_handles_an_existing_empty_retry_row_exactly_once(candidate,monkeypatch):
    import capture_recovery
    from state import connect,unpack
    from capture_queue import claim_resume
    from test_capture_resume import interrupted
    from server import create_app
    root,row=candidate;operation,_,_=interrupted(root,create_app(root,start_worker=False),row)
    error='Operation failed (OperationalError); staged evidence retained'
    with connect(root) as store:
        store.db.execute('UPDATE operations SET error=? WHERE id=?',(error,operation))
        store.db.execute('INSERT INTO capture_retries VALUES (?,4,NULL,0)',(operation,));store.db.commit()
    monkeypatch.setattr(capture_recovery,'now',lambda:'2026-11-15T00:00:00+00:00')
    capture_recovery.schedule(root)
    with connect(root) as store:
        retry=store.db.execute('SELECT * FROM capture_retries WHERE operation=?',(operation,)).fetchone()
        assert retry['retry_at'] is not None and retry['attempts']==4
        store.db.execute("UPDATE capture_retries SET retry_at='2000-01-01'");store.db.commit()
    capture_recovery.schedule(root)
    with connect(root) as store:
        claim_resume(store,operation)
        store.db.execute("UPDATE operations SET state='interrupted',error=? WHERE id=?",(error,operation));store.db.commit()
    capture_recovery.schedule(root)
    with connect(root) as store:
        saved=store.db.execute('SELECT state FROM operations WHERE id=?',(operation,)).fetchone()
        assert saved['state']=='interrupted'
        assert store.db.execute('SELECT retry_at FROM capture_retries WHERE operation=?',(operation,)).fetchone()[0] is None

    # A later worker shutdown is a new transient failure, even after the legacy
    # migration allowance was consumed. It must still recover automatically.
    with connect(root) as store:
        from capture_failures import WORKER_INTERRUPTED
        store.db.execute('UPDATE operations SET error=? WHERE id=?',(WORKER_INTERRUPTED,operation));store.db.commit()
    capture_recovery.schedule(root)
    with connect(root) as store:
        assert store.db.execute('SELECT retry_at FROM capture_retries WHERE operation=?',(operation,)).fetchone()[0]
