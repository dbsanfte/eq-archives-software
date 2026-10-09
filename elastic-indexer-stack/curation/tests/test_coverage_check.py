import json
import subprocess

import pytest

from common import digest
from coverage_check import refresh
from review import apply_decisions
from review import record
from server import create_app
from state import connect
from conftest import add_candidate, manifest_for
from test_server import call


def archive(tmp_path, files):
    repo=tmp_path/'archive'
    repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    for path in files:
        target=repo/'websites'/path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text('EverQuest archive source')
    subprocess.run(['git','-C',str(repo),'add','websites'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture'],check=True)
    return repo


@pytest.mark.parametrize('new_state', ['capturing', 'rejected', 'approved_waiting_batch'])
def test_slow_coverage_refresh_cannot_overwrite_a_concurrent_claim_or_decision(tmp_path, monkeypatch, new_state):
    root = tmp_path / 'state'
    row = add_candidate(root)
    repo = archive(tmp_path, ['other.example/20000101000000/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO', str(repo))
    refresh(root)
    changed = {'site_check': {'status': 'new_site', 'complete': True}, 'capture': {'batch_id': 'a' * 32}}
    def delayed_check(*args, **kwargs):
        with connect(root) as concurrent:
            concurrent.db.execute('UPDATE candidates SET state=?,coverage=?,decision=? WHERE id=?',
                                  (new_state, json.dumps(changed), json.dumps({'decision': 'approve'}), row['id']))
            concurrent.db.commit()
        return {'status': 'already_archived', 'complete': True}
    monkeypatch.setattr('coverage_check.SiteInventory.check', delayed_check)
    refresh(root)
    with connect(root) as store:
        saved = store.candidates()[0]
        assert saved['state'] == new_state
        assert json.loads(saved['coverage']) == changed
        assert json.loads(saved['decision']) == {'decision': 'approve'}
        assert not store.db.execute("SELECT 1 FROM events WHERE action='site_coverage_changed'").fetchone()


@pytest.mark.parametrize('archive_host', ['mythiran.com', 'www.mythiran.com:80', 'mythiran.com:443',
                                         'www.mythiran.com_80', 'mythiran.com_443'])
def test_existing_approved_mythiran_is_removed_from_recommendations_and_cannot_capture_or_publish(tmp_path,monkeypatch,archive_host):
    root=tmp_path/'state'
    row=add_candidate(root,url='http://www.mythiran.com/')
    decision={'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'}
    with connect(root) as store: apply_decisions(store,[decision])
    repo=archive(tmp_path,[archive_host+'/19990918084825/research/spells.html'])
    monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    app=create_app(root,start_worker=False)
    assert call(app,'GET','/api/queue').json()['total']==0
    current=call(app,'GET','/api/queue?filter=all').json()['candidates'][0]
    assert current['state']=='already_archived' and current['decision']['decision']=='approve'
    assert current['coverage']['site_check']['archive_path']=='websites/'+archive_host
    assert call(app,'POST','/api/decisions',[decision]).status_code==409
    assert call(app,'POST','/api/capture',{'ids':[row['id']]}).status_code==409
    manifest=manifest_for(row)
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,'now','now')",(manifest['batch_id'],json.dumps(manifest),digest(manifest)))
        store.db.commit()
    assert call(app,'POST','/api/publish',{'id':manifest['batch_id'],'manifest_sha256':digest(manifest)}).status_code==409
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0


def test_shared_account_duplicates_are_blocked_while_new_account_approvals_are_preserved(tmp_path,monkeypatch):
    root=tmp_path/'state'
    known=add_candidate(root,url='http://www.geocities.com/alice/eq/news.html')
    novel=add_candidate(root,url='https://geocities.com/bob/eq/news.html')
    with connect(root) as store:
        apply_decisions(store,[{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'} for row in (known,novel)])
    repo=archive(tmp_path,['geocities.com/20000101000000/alice/eq/guide.html'])
    monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    refresh(root)
    with connect(root) as store:
        values={row['id']:row['state'] for row in store.candidates()}
        assert values=={known['id']:'already_archived',novel['id']:'approved_waiting_batch'}
        before=store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    refresh(root)
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==before


def test_aliases_in_old_queue_keep_only_one_approval_candidate(tmp_path,monkeypatch):
    root=tmp_path/'state'
    for url in ('http://guild.example/','https://www.guild.example/eq/'):
        add_candidate(root,url=url)
    repo=archive(tmp_path,['other.example/20000101000000/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    refresh(root)
    with connect(root) as store:
        assert sorted(row['state'] for row in store.candidates())==['approval_pending','duplicate_candidate']


def test_published_batch_blocks_alias_before_archive_checkout_is_updated(tmp_path,monkeypatch):
    root=tmp_path/'state'
    published=add_candidate(root,url='http://guild.example/')
    alias=add_candidate(root,url='https://www.guild.example/eq/')
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='published' WHERE id=?",(published['id'],))
        store.db.commit()
    repo=archive(tmp_path,['other.example/20000101000000/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    refresh(root)
    with connect(root) as store:
        current=store.db.execute('SELECT state,coverage FROM candidates WHERE id=?',(alias['id'],)).fetchone()
        assert current['state']=='already_archived'
        assert json.loads(current['coverage'])['site_check']['published_candidate']==published['id']


def test_corrected_legacy_board_scope_requires_reapproval_and_retains_prior_decision(tmp_path,monkeypatch):
    root=tmp_path/'state'
    row=add_candidate(root,url='http://server3.ezboard.com/beverquestmonks.html')
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET scope='http://server3.ezboard.com/' WHERE id=?",(row['id'],))
        store.db.commit()
        row=record(store,store.candidates()[0])
        # Simulate a historical approval: current approval correctly rejects this old host-wide scope.
        grant={'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'}
        store.db.execute("UPDATE candidates SET state='approved_waiting_batch',decision=? WHERE id=?", (json.dumps(grant),row['id']))
        store.db.commit()
    repo=archive(tmp_path,['server3.ezboard.com/20000101000000/botherguild/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    refresh(root)
    with connect(root) as store:
        current=store.candidates()[0]
        assert current['state']=='approval_pending' and current['decision'] is None
        assert current['scope']=='http://server3.ezboard.com/beverquestmonks/'
        correction=json.loads(current['coverage'])['scope_corrections'][0]
        assert correction['previous_decision']['decision']=='approve'
        assert correction['previous_scope']=='http://server3.ezboard.com/'


@pytest.mark.parametrize('previously_unverified', [False, True])
def test_partial_recheck_cannot_restore_approval_invalidated_by_scope_correction(tmp_path,monkeypatch,previously_unverified):
    from site_inventory import SiteInventory
    root=tmp_path/'state'
    row=add_candidate(root,url='http://pub6.ezboard.com/bthemagicianstower.html')
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET scope='http://pub6.ezboard.com/' WHERE id=?",(row['id'],))
        store.db.commit()
        row=record(store,store.candidates()[0])
        # Simulate a historical approval: current approval correctly rejects this old host-wide scope.
        grant={'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'}
        store.db.execute("UPDATE candidates SET state='approved_waiting_batch',decision=? WHERE id=?", (json.dumps(grant),row['id']))
        store.db.commit()
        if previously_unverified:
            coverage=json.loads(store.candidates()[0]['coverage'])
            coverage['previous_state']='approved_waiting_batch'
            store.db.execute("UPDATE candidates SET state='coverage_unverified',coverage=? WHERE id=?",(json.dumps(coverage),row['id']))
            store.db.commit()
    repo=archive(tmp_path,['pub6.ezboard.com/20000101000000/botherguild/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    check=SiteInventory.check
    monkeypatch.setattr(SiteInventory,'check',lambda self,*args,**kwargs:{'status':'inventory_partial','complete':False})
    refresh(root)
    with connect(root) as store:
        assert store.candidates()[0]['state']=='coverage_unverified'
        assert store.candidates()[0]['decision'] is None
    monkeypatch.setattr(SiteInventory,'check',check)
    refresh(root,force=True)
    with connect(root) as store:
        current=store.candidates()[0]
        assert current['state']=='approval_pending' and current['decision'] is None
        assert json.loads(current['coverage'])['scope_corrections'][0]['previous_decision']['decision']=='approve'


def test_repeated_partial_rechecks_preserve_unchanged_approval(tmp_path,monkeypatch):
    from site_inventory import SiteInventory
    root=tmp_path/'state'
    row=add_candidate(root,url='http://guild.example/eq/news.html')
    with connect(root) as store:
        apply_decisions(store,[{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'}])
        decision=store.candidates()[0]['decision']
    repo=archive(tmp_path,['other.example/20000101000000/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    check=SiteInventory.check
    for _ in range(2):
        monkeypatch.setattr(SiteInventory,'check',lambda self,*args,**kwargs:{'status':'inventory_partial','complete':False})
        refresh(root)
        with connect(root) as store:
            assert store.candidates()[0]['state']=='coverage_unverified'
        monkeypatch.setattr(SiteInventory,'check',check)
        refresh(root,force=True)
        with connect(root) as store:
            current=store.candidates()[0]
            assert current['state']=='approved_waiting_batch' and current['decision']==decision


def test_recheck_api_reports_partial_progress_and_verified_outcome(tmp_path,monkeypatch):
    from site_inventory import SiteInventory
    root=tmp_path/'state'
    row=add_candidate(root,url='http://geocities.com/new-account/eq.html')
    with connect(root) as store:
        store.db.execute('UPDATE candidates SET coverage=? WHERE id=?',
                         (json.dumps({'status':'inventory_partial','complete':False}),row['id']))
        store.db.commit()
    repo=archive(tmp_path,[f'geocities.com/2000010{index}000000/other-{index}/index.html' for index in range(1,7)])
    monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    monkeypatch.setattr('coverage_check.SiteInventory',lambda archive:SiteInventory(archive,max_trees=2))
    app=create_app(root,start_worker=False)
    payload={'id':row['id'],'manifest_sha256':row['manifest_sha256']}
    assert call(app,'POST','/api/coverage',{**payload,'manifest_sha256':'stale'}).status_code==409
    result=call(app,'POST','/api/coverage',payload).json()
    assert result['checked']==row['id'] and result['state']=='coverage_unverified'
    check=result['coverage']
    assert check['status']=='inventory_partial' and check['complete'] is False
    assert check['reason']=='tree_limit' and check['retryable'] is True
    assert 0<check['progress']['checked']<check['progress']['total']==6
    previous=check['progress']['checked']
    for _ in range(6):
        result=call(app,'POST','/api/coverage',payload).json()
        assert result['coverage']['progress']['checked']>previous
        previous=result['coverage']['progress']['checked']
        if result['coverage']['complete']:break
    assert result['coverage']['status']=='new_site' and result['state']=='approval_pending'
    with connect(root) as store:
        current=record(store,store.candidates()[0])
        assert current['coverage']['complete'] is False  # Original page inventory is separate evidence.
        assert current['coverage']['site_check']['complete'] is True
        assert current['manifest_sha256']==row['manifest_sha256'] and current['decision'] is None
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0


def test_recheck_api_cannot_claim_success_without_archive_metadata(candidate,monkeypatch):
    root,row=candidate
    monkeypatch.delenv('ARCHIVE_REPO',raising=False)
    app=create_app(root,start_worker=False)
    response=call(app,'POST','/api/coverage',{'id':row['id'],'manifest_sha256':row['manifest_sha256']})
    assert response.status_code==409 and 'not configured' in response.json()['error']
