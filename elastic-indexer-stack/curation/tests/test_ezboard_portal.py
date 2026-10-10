import json
from pathlib import Path
from unittest.mock import patch

import pytest

from common import CrawlError, digest, site_identity
from captures import capture_sites, check_manifest
from conftest import add_candidate, manifest_for
from discovery import Archive
from ezboard import board_url
from ezboard_discovery import resolve
from ezboard_portal import consolidate
from manual import prepare
from review import apply_decisions, checked_sources, record
from server import create_app
from site_inventory import SiteInventory
from state import Lane, connect, unpack
from test_coverage_check import archive
from test_server import call
from worker import Worker
from test_ezboard import BOARD, FORUM, MOVED, STAMP, FakeDownloader, html


def test_board_identity_ignores_server_but_never_source_url_or_other_boards():
    assert site_identity(BOARD) == site_identity('https://server3.ezboard.com/beqasylum.html')
    assert site_identity(BOARD) != site_identity('http://pub4.ezboard.com/beqasylum2')
    assert site_identity('http://guild.example/') != site_identity(BOARD)


def test_coverage_finds_board_on_another_server_and_resumes_bounded_metadata(tmp_path):
    repo = archive(tmp_path, ['pub110.ezboard.com/20020419220745/beqasylum/index.html',
                              'server3.ezboard.com/20000101000000/bother/index.html'])
    with connect(tmp_path/'state') as store:
        reader = Archive(repo, store)
        small = SiteInventory(reader, max_trees=1)
        first = small.check(BOARD)
        assert first['status'] == 'inventory_partial' and first['retryable']
        result = SiteInventory(reader).check(BOARD)
        assert result['status'] == 'already_archived'
        assert result['archive_host'] == 'pub110.ezboard.com'
        assert SiteInventory(reader).check('http://pub9.ezboard.com/bnew')['status'] == 'new_site'
        assert SiteInventory(reader, max_trees=0).check(BOARD)['status'] == 'already_archived'


def test_forum_only_archive_is_not_wrongly_reported_as_new(tmp_path):
    repo = archive(tmp_path, ['pub110.ezboard.com/20020419220745/feqasylumgeneral/index.html'])
    with connect(tmp_path/'state') as store:
        result = SiteInventory(Archive(repo, store)).check(BOARD)
        assert result['status'] == 'inventory_partial' and result['reason'] == 'ezboard_owner_unverified'


def test_many_board_checks_share_one_host_snapshot(tmp_path):
    repo = archive(tmp_path, ['PUB110.ezboard.com/20020419220745/beqasylum/index.html',
                              'server3.ezboard.com/20000101000000/bother/index.html'])
    with connect(tmp_path/'state') as store:
        reader = Archive(repo, store)
        statements = []
        store.db.set_trace_callback(statements.append)
        inventory = SiteInventory(reader)
        assert inventory.check(BOARD)['archive_host'] == 'PUB110.ezboard.com'
        assert inventory.check('http://pub9.ezboard.com/bother')['status'] == 'already_archived'
        for number in range(100):
            assert inventory.check(f'http://pub9.ezboard.com/bnew{number}')['status'] == 'new_site'
        assert inventory.check(BOARD)['status'] == 'already_archived'
        host_reads = [sql for sql in statements if sql.strip().upper().startswith('SELECT')
                      and 'FROM HOSTS' in sql.upper()]
        assert len(host_reads) == 1, 'A coverage pass must not rescan every host for each board'
        # A later pass still takes a fresh snapshot; exact archive folder case survives.
        statements.clear()
        assert SiteInventory(reader, max_trees=0).check(BOARD)['archive_host'] == 'PUB110.ezboard.com'
        assert sum('FROM HOSTS' in sql.upper() for sql in statements) == 1


def test_existing_deep_suggestions_consolidate_without_copying_sources_or_reapproving(tmp_path,monkeypatch):
    root = tmp_path/'state'
    one = add_candidate(root, url=FORUM, body=html(BOARD))
    two = add_candidate(root, url=MOVED, body=html('http://pub110.ezboard.com/beqasylum'))
    approved = add_candidate(root, url='http://server3.ezboard.com/fothersgeneral', body=html('http://server3.ezboard.com/bothers'))
    with connect(root) as store:
        apply_decisions(store, [{'id': approved['id'], 'manifest_sha256': approved['manifest_sha256'], 'decision': 'approve'}])
    before = {c['path']: (root/c['path']).stat().st_ino for row in (one,two,approved) for c in row['captures']}
    consolidate(root); consolidate(root)
    repo=archive(tmp_path,['known.example/20000101000000/index.html']);monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    from coverage_check import refresh
    from manual import existing_site
    refresh(root)
    with connect(root) as store:
        rows = [record(store,row) for row in store.candidates()]
        boards = [row for row in rows if row['state'] == 'approval_pending']
        assert len(boards) == 1 and board_url(boards[0]['url'])
        assert existing_site(store,MOVED)['id']==boards[0]['id']
        assert boards[0]['scope_mode'] == 'ezboard' and boards[0]['decision'] is None
        checked_sources(store, boards[0])
        assert sum(row['state'] == 'duplicate_candidate' for row in rows) == 2
        old = next(row for row in rows if row['id'] == approved['id'])
        assert old['state'] == 'approved_waiting_batch' and old['manifest_sha256'] == approved['manifest_sha256']
        assert {path: (root/path).stat().st_ino for path in before} == before


def test_published_legacy_thread_blocks_duplicate_board_without_changing_its_scope(tmp_path):
    from coverage_check import require_new
    from manual import existing_site
    root=tmp_path/'state';old=add_candidate(root,url=MOVED,body=html(BOARD))
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='published' WHERE id=?", (old['id'],));store.db.commit()
    consolidate(root)
    with connect(root) as store:
        assert existing_site(store,BOARD)['id']==old['id']
        assert store.candidates()[0]['scope']==old['scope']
        with pytest.raises(CrawlError,match='already published'):
            require_new(store,BOARD)


def test_unresolved_legacy_message_is_saved_without_guessing_or_spending(tmp_path):
    root=tmp_path/'state'; row=add_candidate(root,url=FORUM)
    consolidate(root)
    app=create_app(root,start_worker=False)
    result=call(app,'GET','/api/queue?filter=saved').json()['candidates'][0]
    assert result['coverage']['ezboard_parent_required']
    assert call(app,'POST','/api/restore',{'id':row['id'],'manifest_sha256':result['manifest_sha256']}).status_code == 409
    with connect(root) as store:
        assert not store.db.execute('SELECT 1 FROM attempts').fetchone()


def test_legacy_port_profiles_leave_candidates_without_changing_approved_or_active_work(tmp_path):
    from state import enqueue
    root=tmp_path/'state'
    url='http://server2.ezboard.com:8080/ufscnitro.showPublicProfile'
    pending=add_candidate(root,url=url,grade=None)
    approved=add_candidate(root,url=url.replace('ufscnitro','uapproved'))
    active=add_candidate(root,url=url.replace('ufscnitro','uactive'),grade=None)
    with connect(root) as store:
        apply_decisions(store,[{'id':approved['id'],'manifest_sha256':approved['manifest_sha256'],'decision':'approve'}])
        enqueue(store,'candidate_check',{'id':active['id'],'manifest_sha256':active['manifest_sha256'],'max_usd':2})
    consolidate(root)
    with connect(root) as store:
        rows={row['id']:record(store,row) for row in store.candidates()}
        assert rows[pending['id']]['state']=='deferred'
        assert rows[pending['id']]['coverage']['ezboard_parent_required']
        assert rows[approved['id']]['state']=='approved_waiting_batch'
        for original in (pending,approved,active):
            assert rows[original['id']]['manifest_sha256']==original['manifest_sha256']
        assert rows[active['id']]['state']=='sampled'
        assert not store.db.execute('SELECT 1 FROM attempts').fetchone()


@pytest.mark.parametrize('port',['',':8080'])
def test_profile_evidence_checks_are_rejected_before_queueing_downloads_or_luna(tmp_path,port):
    root=tmp_path/'state'
    row=add_candidate(root,url='http://server2.ezboard.com'+port+'/ufscnitro.showPublicProfile',grade=None)
    app=create_app(root,start_worker=False)
    result=call(app,'POST','/api/check-candidate',{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'max_usd':2})
    assert result.status_code==409 and 'top-level board URL' in result.json()['error']
    with connect(root) as store:
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()
        assert not store.db.execute('SELECT 1 FROM attempts').fetchone()


@pytest.mark.parametrize('action',['approve','check'])
def test_consolidation_rechecks_decisions_made_while_it_reads_sources(tmp_path,action):
    from captures import verified_source
    from state import enqueue
    root=tmp_path/'state'
    row=add_candidate(root,url='http://server2.ezboard.com:8080/urace.showPublicProfile')
    def concurrent_decision(*args):
        data=verified_source(*args)
        with connect(root) as store:
            payload={'id':row['id'],'manifest_sha256':row['manifest_sha256']}
            if action=='approve':apply_decisions(store,[{**payload,'decision':'approve'}])
            else:enqueue(store,'candidate_check',{**payload,'max_usd':2})
        return data
    with patch('captures.verified_source',side_effect=concurrent_decision):consolidate(root)
    with connect(root) as store:
        current=record(store,store.candidates()[0])
        assert current['state']==('approved_waiting_batch' if action=='approve' else 'approval_pending')
        assert current['manifest_sha256']==row['manifest_sha256']


class ResolverDownloader:
    def __init__(self, *args): self.calls=[]; self.closed=False
    def call(self, job):
        self.calls.append(job)
        if job['op']=='list':
            return {'captures':[{'url':job['url'],'timestamp':STAMP,'digest':'fixture','length':'123'}]}
        data=html(BOARD)
        Path(job['destination']).write_bytes(data)
        return {'url':job['url'],'timestamp':STAMP,'requested_timestamp':STAMP,'bytes':len(data),
                'sha256':digest(data),'content_type':'text/html'}
    def close(self): self.closed=True


def test_manual_message_resolves_once_to_board_and_reuses_exact_source_before_grading(tmp_path,monkeypatch):
    repo=archive(tmp_path,['known.example/20000101000000/index.html']);monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    with connect(tmp_path/'state') as store:
        downloader=ResolverDownloader()
        operation={'id':'a'*32,'payload':{'target':{'url':MOVED,'submitted_url':'https://web.archive.org/web/2000/'+MOVED}}}
        assert prepare(store,operation,downloader)
        row=record(store,store.candidates()[0])
        assert row['url']==BOARD and row['scope_mode']=='ezboard'
        assert row['captures'][0]['url']==MOVED
        assert row['captures'][0]['sha256']==digest(html(BOARD))
        assert row['evidence'][0]['linked_url']==MOVED
        assert resolve(store,MOVED,downloader)==BOARD and len(downloader.calls)==2
        assert not store.db.execute('SELECT 1 FROM attempts').fetchone()


def test_manual_message_on_another_shard_reuses_existing_board_without_luna(tmp_path,monkeypatch):
    root=tmp_path/'state'; existing=add_candidate(root,url=BOARD,body=html())
    repo=archive(tmp_path,['known.example/20000101000000/index.html']);monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    app=create_app(root,start_worker=False)
    response=call(app,'POST','/api/submit-site',{'url':MOVED,'max_usd':2})
    assert response.status_code==202
    worker=Worker(root)
    try:
        with patch('worker.Downloader',ResolverDownloader), patch('worker.grade') as paid, patch('worker.sample') as sample:
            worker.operation(Lane.CANDIDATES); paid.assert_not_called(); sample.assert_not_called()
        with connect(root) as store:
            operation=unpack(store.db.execute('SELECT * FROM operations').fetchone())
            assert operation['state']=='completed', operation
            assert operation['result']['candidate_id']==existing['id']
            assert len(store.candidates())==1
        second=call(app,'POST','/api/submit-site',{'url':MOVED.replace('pub110','server3'),'max_usd':2})
        assert second.status_code==200 and second.json()['candidate_id']==existing['id']
    finally:worker.lease.close()


def test_whole_board_capture_uses_catalog_and_manifest_rechecks_membership(tmp_path,monkeypatch):
    monkeypatch.delenv('ARCHIVE_REPO',raising=False)
    root=tmp_path/'state'; row=add_candidate(root,url=BOARD,body=html(FORUM))
    row.update(scope=BOARD,scope_mode='ezboard')
    FakeDownloader.instances=[];FakeDownloader.failure=None
    FakeDownloader.catalogs={
        (BOARD,'19990101000000',None): {'captures':[{'url':BOARD,'timestamp':STAMP,'digest':'D','length':'1'}],'resume_key':None},
        ('http://pub4.ezboard.com/feqasylum','19990101000000',None): {'captures':[{'url':FORUM,'timestamp':STAMP,'digest':'D','length':'1'}],'resume_key':None}}
    FakeDownloader.sources={(BOARD,STAMP):html(FORUM),(FORUM,STAMP):html(BOARD)}
    result=capture_sites(root,'a'*32,[row],downloader_factory=FakeDownloader)
    assert len(result['captures'])==3 and result['ezboard']['coverage']['state']=='complete'
    assert {c['url'] for c in result['captures']}=={BOARD,FORUM}
    reused=next(c for c in result['captures'] if c['timestamp']=='20000101000000')
    assert (root/reused['path']).stat().st_ino==(root/row['captures'][0]['path']).stat().st_ino
    check_manifest(root,result)
    bad=dict(result['captures'][1]);bad['url']='http://pub4.ezboard.com/feqasylum2general'
    body=html('http://pub4.ezboard.com/beqasylum2');(root/bad['path']).write_bytes(body)
    bad.update(bytes=len(body),sha256=digest(body));result['captures'][1]=bad
    with pytest.raises(CrawlError):check_manifest(root,result)
