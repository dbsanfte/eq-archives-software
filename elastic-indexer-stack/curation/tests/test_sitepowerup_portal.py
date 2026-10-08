import json
from unittest.mock import patch

import pytest

from acquisition import sample
from common import CrawlError, digest
from conftest import add_candidate
from crawler import parser
from discovery import Archive
from captures import capture_sites, check_manifest
from manual import prepare
from review import apply_decisions, checked_sources, record
from server import create_app
from site_inventory import SiteInventory
from sitepowerup_portal import consolidate
from state import connect, enqueue, unpack
from test_coverage_check import archive
from test_server import call
from test_sitepowerup import BOARD, MESSAGE, PREFIX, STAMP, Downloader, html, listing


@pytest.fixture(autouse=True)
def transport():
    Downloader.catalogs, Downloader.sources, Downloader.instances, Downloader.failure = {}, {}, [], None


def test_shared_legacy_inventory_finds_boards_and_resumes_without_blob_reads(tmp_path):
    repo = archive(tmp_path, ['www.sitepowerup.com/19990830011719/mb/view.asp_BoardID=102010',
                              'www.sitepowerup.com/20010101000000/mb/view.asp_Action=Reply_and_BoardID=104254_and_Reply=2',
                              'sitepowerup.com/20010101000000/mb/view.asp?Action=Post&BoardID=999',
                              'www.sitepowerup.com/20010101000000/user/login.asp_BoardID=123'])
    with connect(tmp_path/'state') as store:
        reader = Archive(repo, store, max_entries=1)
        reader.files('www.sitepowerup.com')  # The ordinary page inventory is deliberately incomplete.
        with patch.object(reader, 'blob', side_effect=AssertionError('No source reads permitted')):
            first = SiteInventory(reader,max_trees=1).check(MESSAGE)
            assert first['status']=='inventory_partial' and first['retryable']
            result = SiteInventory(reader).check(MESSAGE)
            assert result['status']=='already_archived' and result['archive_path'].endswith('view.asp_BoardID=102010')
            assert SiteInventory(reader,max_trees=0).check(BOARD)['status']=='already_archived'
            assert SiteInventory(reader).check(BOARD.replace('102010','104254'))['status']=='already_archived'
            assert SiteInventory(reader).check(BOARD.replace('102010','999'))['status']=='new_site'
            # Completed scans and positive matches are shared across all boards.
            assert SiteInventory(reader,max_trees=0).check(BOARD.replace('102010','9999'))['status']=='new_site'


def test_consolidation_retains_sources_and_protected_scopes_without_replaying_work(tmp_path):
    root=tmp_path/'state'
    parent=add_candidate(root,url=BOARD,body=html())
    child=add_candidate(root,url=MESSAGE,body=html(reply='12155'))
    another=add_candidate(root,url=MESSAGE.replace('12155','12154'),body=html(reply='12154'))
    protected=add_candidate(root,url=MESSAGE.replace('102010','777'),body=html('777',reply='12155'))
    active=add_candidate(root,url=MESSAGE.replace('102010','888'),body=html('888',reply='12155'))
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='indexed' WHERE id=?",(parent['id'],))
        store.db.commit()
        apply_decisions(store,[{'id':protected['id'],'manifest_sha256':protected['manifest_sha256'],'decision':'approve'}])
        enqueue(store,'candidate_check',{'id':active['id'],'manifest_sha256':active['manifest_sha256'],'max_usd':2})
    sources={c['path']:(root/c['path']).stat().st_ino for r in (parent,child,another,protected,active) for c in r['captures']}
    consolidate(root);consolidate(root)
    with connect(root) as store:
        rows={row['id']:record(store,row) for row in store.candidates()}
        assert rows[parent['id']]['state']=='indexed' and rows[parent['id']]['manifest_sha256']==parent['manifest_sha256']
        assert all(rows[r['id']]['state']=='duplicate_candidate' for r in (child,another))
        assert rows[protected['id']]['state']=='approved_waiting_batch' and rows[protected['id']]['manifest_sha256']==protected['manifest_sha256']
        assert rows[active['id']]['manifest_sha256']==active['manifest_sha256']
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==1
        assert store.db.execute("SELECT COUNT(*) FROM events WHERE action='sitepowerup_consolidated'").fetchone()[0]==2
    assert all((root/path).stat().st_ino==inode for path,inode in sources.items())


def test_unapproved_messages_become_one_board_candidate_with_their_exact_evidence(tmp_path):
    root=tmp_path/'state'
    original=add_candidate(root,url=MESSAGE,body=html(reply='12155'))
    second=add_candidate(root,url=MESSAGE.replace('12155','12154'),body=html(reply='12154'))
    form=add_candidate(root,url=BOARD.replace('102010','555').replace('Display','Post'))
    consolidate(root)
    with connect(root) as store:
        rows=[record(store,row) for row in store.candidates()]
        board=next(row for row in rows if row['url']==BOARD)
        assert board['scope_mode']=='sitepowerup' and board['decision'] is None
        assert board['captures'] in (original['captures'],second['captures']) and board['sitepowerup']=='102010'
        checked_sources(store,board)
        assert next(row for row in rows if row['id']==form['id'])['state']=='rejected'
    app=create_app(root,start_worker=False)
    reply=call(app,'POST','/api/submit-site',{'url':MESSAGE.replace('12155','999'),'max_usd':2})
    assert reply.status_code==200 and reply.json()['candidate_id']==board['id']


def test_consolidation_preserves_a_saved_exact_page_scope_before_approval(tmp_path):
    root=tmp_path/'state'
    chosen=add_candidate(root,url=MESSAGE,body=html(reply='12155'))
    sibling=add_candidate(root,url=MESSAGE.replace('12155','12154'),body=html(reply='12154'))
    with connect(root) as store:
        store.db.execute('UPDATE candidates SET coverage=? WHERE id=?',
                         (json.dumps({'scope_mode':'page'}),chosen['id']))
        store.db.commit()
        chosen=record(store,store.db.execute('SELECT * FROM candidates WHERE id=?',(chosen['id'],)).fetchone())
    consolidate(root)
    with connect(root) as store:
        rows={row['id']:record(store,row) for row in store.candidates()}
        assert len(rows)==2
        assert rows[chosen['id']]['manifest_sha256']==chosen['manifest_sha256']
        assert rows[chosen['id']]['scope_mode']=='page' and rows[chosen['id']]['state']=='approval_pending'
        assert rows[sibling['id']]['coverage']['duplicate_of']==chosen['id']


def test_manual_deep_link_checks_and_samples_only_its_board_with_exact_url_variants(tmp_path,monkeypatch):
    repo=archive(tmp_path,['known.example/20000101000000/index.html']);monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    root=tmp_path/'state';submitted='https://web.archive.org/web/2000/'+MESSAGE
    with connect(root) as store:
        operation={'id':'a'*32,'payload':{'target':{'url':MESSAGE,'submitted_url':submitted}}}
        assert prepare(store,operation)
        row=record(store,store.candidates()[0])
        assert row['url']==row['scope']==BOARD and row['scope_mode']=='sitepowerup'
        assert row['evidence'][0]['linked_url']==MESSAGE and row['evidence'][0]['source_url']==submitted
        first='http://sitepowerup.com/mb/view.asp?boardid=102010'
        second=first+'&Page=2'
        listing('http://www.sitepowerup.com/mb/view.asp?BoardID=102010',[(first,STAMP),(second,STAMP)])
        Downloader.sources={(first,STAMP):html(),(second,STAMP):html()}
        args=parser().parse_args(['--work-dir',str(root),'sample','--max-candidates','1'])
        downloader=Downloader(store,args)
        sample(args,store,downloader=downloader)
        row=record(store,store.candidates()[0])
        assert row['state']=='sampled' and len(row['captures'])==2
        assert [c['url'] for c in row['captures']]==[first,second]
        assert len({c['path'] for c in row['captures']})==2
        assert all((root/c['path']).read_bytes()==html() for c in row['captures'])
        assert not store.db.execute('SELECT 1 FROM attempts').fetchone()


def test_manual_archived_board_retires_without_sampling_and_bad_ids_pause(tmp_path,monkeypatch):
    repo=archive(tmp_path,['www.sitepowerup.com/19990830011719/mb/view.asp_BoardID=102010']);monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    with connect(tmp_path/'state') as store:
        payload={'id':'a'*32,'payload':{'target':{'url':MESSAGE,'submitted_url':MESSAGE}}}
        assert prepare(store,payload) is False
        assert store.candidates()[0]['state']=='already_archived'
        with pytest.raises(CrawlError,match='numeric BoardID'):
            prepare(store,{'id':'b'*32,'payload':{'target':{'url':BOARD+'&boardid=999','submitted_url':BOARD}}})


def test_whole_board_capture_seeds_reviewed_sources_and_checks_every_publication_source(tmp_path):
    root=tmp_path/'state';row=add_candidate(root,url=BOARD,body=html())
    with connect(root) as store:
        store.db.execute('UPDATE candidates SET coverage=?',(json.dumps({'scope_mode':'sitepowerup'}),));store.db.commit()
        row=record(store,store.candidates()[0])
    listing(BOARD,[(BOARD,STAMP)])
    listing(PREFIX,[(MESSAGE,'20061231235959')],level=2)
    Downloader.sources={(BOARD,STAMP):html(),(MESSAGE,'20061231235959'):html(reply='12155')}
    progress=[]
    manifest=capture_sites(root,'a'*32,[row],Downloader,progress.append)
    assert manifest['sitepowerup']['board']=='102010' and manifest['sitepowerup']['coverage']['state']=='complete'
    assert manifest['capture_window']['to']=='20061231235959' and len(manifest['captures'])==3  # Seed has a distinct date.
    assert progress[-1]['phase']=='ready_for_review' and manifest['indexing']['enrichment']
    check_manifest(root,manifest)
    bad={**manifest,'sites':[{**row,'scope':BOARD.replace('102010','999')}]}
    with pytest.raises(CrawlError,match='scope changed'):check_manifest(root,bad)
    bad={**manifest,'sites':[row,{**row,'id':'different'}]}
    with pytest.raises(CrawlError,match='independent'):check_manifest(root,bad)
    with pytest.raises(CrawlError,match='independently'):capture_sites(root,'b'*32,[row,row],Downloader)
    altered={**manifest,'captures':[dict(c) for c in manifest['captures']]}
    altered['captures'][0]['candidate_id']='missing'
    with pytest.raises(CrawlError,match='outside'):check_manifest(root,altered)
