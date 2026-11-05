import hashlib
import json
import sqlite3
import threading
import time

import pytest

from indexer.reindex_diagnostics import Ledger,main,reason,source_hash
from indexer.sqlite_state import SQLiteState,locked


def test_rejections_survive_restart_and_do_not_store_upstream_payloads(tmp_path):
    path=tmp_path/'failures.sqlite';ledger=Ledger(path)
    ledger.record('source',model='model',policy='old',source_sha256='a'*64,
                  error=ValueError('private upstream response must never be retained'),job='original-job')
    ledger.close();ledger=Ledger(path);report=ledger.report()
    assert report['summary']=={'pending':1} and ledger.pending_ids()==['source']
    row=report['records'][0]
    assert row['source_sha256']=='a'*64 and row['reason']=='source_or_vector_validation'
    assert row['attempts']==1 and row['job']=='original-job' and row['status']=='pending'
    assert 'private upstream' not in json.dumps(report)


def test_attempts_hashes_versions_and_resolutions_retain_their_history(tmp_path):
    ledger=Ledger(tmp_path/'failures.sqlite')
    for _ in range(2):ledger.record('source',model='model',policy='old',source_sha256='a'*64,error=UnicodeError('bad source'))
    ledger.record('source',model='model',policy='new',source_sha256='b'*64,error=ValueError('bad source'))
    assert ledger.pending_ids()==['source']
    ledger.resolve('source',policy='fixed',source_sha256='c'*64,job='repair')
    assert not ledger.has_pending('source') and ledger.pending_ids()==[]
    rows=ledger.report(status='all')['records'];assert len(rows)==2
    assert {r['source_sha256'] for r in rows}=={'a'*64,'b'*64}
    assert sorted(r['attempts'] for r in rows)==[1,2]
    assert all(r['status']=='resolved' and r['resolution_source_sha256']=='c'*64 and r['resolution_job']=='repair' for r in rows)
    ledger.record('source',model='model',policy='old',source_sha256='a'*64,error=ValueError('again'))
    assert ledger.has_pending('source') and ledger.report()['records'][0]['attempts']==3


def test_real_writer_contention_retries_only_the_metadata_transaction(tmp_path):
    path=tmp_path/'failures.sqlite';ledger=Ledger(path);ledger.record('source',model='model',policy='old',error=UnicodeError('bad'))
    ready=threading.Event()
    def writer():
        db=sqlite3.connect(path);db.execute('BEGIN IMMEDIATE');ready.set();time.sleep(.4);db.commit();db.close()
    thread=threading.Thread(target=writer);thread.start();assert ready.wait(3)
    try:ledger.resolve('source',policy='fixed')
    finally:thread.join()
    assert ledger.report(status='resolved')['summary']=={'resolved':1}


def test_missing_sources_and_reports_remain_explicit_and_bounded(tmp_path):
    ledger=Ledger(tmp_path/'failures.sqlite')
    for i in range(3):ledger.record(str(i),model='model',policy='v8',category='source_unavailable')
    assert ledger.report(limit=2)['summary']=={'pending':3} and len(ledger.report(limit=2)['records'])==2
    assert len(ledger.pending_ids(limit=1))==1
    for kwargs in ({'limit':0},{'limit':1001},{'status':'unknown'}):
        with pytest.raises(ValueError):ledger.report(**kwargs)
    with pytest.raises(ValueError):ledger.pending_ids(1001)
    with pytest.raises(ValueError):ledger.record('id',model='m',policy='p',source_sha256='unverified')
    with pytest.raises(ValueError):ledger.record('id',model='m',policy='p',category='hidden failure')


def test_exception_categories_never_depend_on_retaining_an_upstream_error_body():
    assert reason(ValueError('Could not decode file /private/source with any encoding.'))=='source_decoding_rejected'
    assert reason(FileNotFoundError())=='source_unavailable'
    for name,category in [('ConflictError','concurrent_update'),('AuthenticationError','upstream_authentication'),
                          ('APIConnectionError','upstream_transport')]:
        assert reason(type(name,(Exception,),{})('private response'))==category


def test_source_hash_is_exact_bounded_and_cannot_escape_the_prepared_checkout(tmp_path):
    root=tmp_path/'sources';root.mkdir();path=root/'source';path.write_bytes(b'Complete original bytes\r\n')
    assert source_hash({'id':'source'},root)==hashlib.sha256(path.read_bytes()).hexdigest()
    assert source_hash({'id':'missing'},root) is None
    outside=tmp_path/'private';outside.write_bytes(b'do not read');(root/'link').symlink_to(outside)
    assert source_hash({'id':'link'},root) is None and source_hash({'id':'../private'},root) is None
    with (root/'large').open('wb') as stream:stream.truncate(65*1024*1024)
    assert source_hash({'id':'large'},root) is None


def test_non_contention_database_errors_stay_visible(tmp_path):
    ledger=Ledger(tmp_path/'failures.sqlite');ledger.state.connection().execute('DROP TABLE exceptions')
    with pytest.raises(sqlite3.OperationalError):ledger.record('source',model='m',policy='p',error=ValueError())
    with pytest.raises(ValueError):SQLiteState('relative.sqlite')
    assert not locked(sqlite3.OperationalError('other database failure'))


def test_operator_cli_reports_persistent_pending_and_resolved_records(tmp_path,monkeypatch,capsys):
    path=tmp_path/'failures.sqlite';ledger=Ledger(path);ledger.record('source',model='m',policy='p',error=ValueError())
    monkeypatch.setattr('sys.argv',['diagnostics','--path',str(path),'--status','all','--limit','10']);main()
    assert json.loads(capsys.readouterr().out)['records'][0]['id']=='source'
    monkeypatch.delenv('REINDEX_DIAGNOSTICS_PATH',raising=False);monkeypatch.setattr('sys.argv',['diagnostics'])
    with pytest.raises(SystemExit):main()


def test_bounded_pending_index_falls_back_to_sql_for_large_exception_sets(tmp_path):
    ledger=Ledger(tmp_path/'failures.sqlite');db=ledger.state.connection()
    with ledger.state.transaction():
        db.executemany("INSERT INTO exceptions VALUES (?,'m','p','','source_unavailable','',0,0,1,'','pending',NULL,NULL,NULL,NULL)",
                       ((str(i),) for i in range(10001)))
    ledger.sync();assert ledger.has_pending('10000') and not ledger.has_pending('missing')
    assert len(ledger.pending_ids())==1000
