import json

import pytest

from common import digest
from conftest import add_candidate, manifest_for
from server import create_app
from state import connect
from test_server import call


def save_review(root, row, state, candidate_state='published'):
    manifest=manifest_for(row,digest(row['id'])[:32])
    with connect(root) as store:
        store.db.execute('INSERT INTO batches VALUES (?,?,?,?,NULL,NULL,NULL,?,?)',
            (manifest['batch_id'],state,json.dumps(manifest),digest(manifest),'2026-01-01','2026-01-02'))
        store.db.execute('UPDATE candidates SET state=?,coverage=? WHERE id=?',
            (candidate_state,json.dumps({'capture':{'review_id':manifest['batch_id'],'batch_id':manifest['batch_id'],'completed_at':'2026-01-02'}}),row['id']))
        store.db.commit()
    return manifest


def test_portal_stages_are_exclusive_and_completed_sites_retire_even_with_legacy_candidate_states(tmp_path):
    root=tmp_path/'state'
    expected={}
    for index,(state,stage,candidate_state) in enumerate([
        ('awaiting_review','review','captured_awaiting_review'),
        ('publication_requested','indexing','captured_awaiting_review'),
        ('published_waiting_index','indexing','published'),('indexing','indexing','published'),
        ('index_failed','indexing','published'),('indexed','history','published'),('indexed','history','capturing'),
        ('indexing_declined','history','indexing_declined')]):
        row=add_candidate(root,url=f'http://capture-{index}.example/')
        save_review(root,row,state,candidate_state)
        expected[row['id']]=stage
    for index,(state,stage) in enumerate([('approval_pending','candidates'),('deferred','saved'),('rejected','history'),
                                         ('already_archived','history'),('duplicate_candidate','history'),('approved_waiting_batch','queued'),('capturing','capturing')]):
        row=add_candidate(root,url=f'http://candidate-{index}.example/')
        with connect(root) as store:
            store.db.execute('UPDATE candidates SET state=? WHERE id=?',(state,row['id']));store.db.commit()
        expected[row['id']]=stage
    app=create_app(root,start_worker=False)
    actual={}
    for stage in ('candidates','queued','capturing','review','indexing','saved','history'):
        response=call(app,'GET',f'/api/queue?filter={stage}').json()
        assert response['stage_counts'][stage]==len(response['candidates'])
        for row in response['candidates']:
            assert row['id'] not in actual
            assert row['stage']==stage
            actual[row['id']]=stage
    assert actual==expected
    assert sum(response['stage_counts'].values())==len(expected)


def test_stages_and_counts_use_all_review_metadata_not_the_recent_batch_limit(tmp_path):
    root=tmp_path/'state'
    first=add_candidate(root)
    manifest=save_review(root,first,'indexed')
    with connect(root) as store:
        for index in range(120):
            store.db.execute("INSERT INTO batches VALUES (?,'capturing',NULL,NULL,NULL,NULL,NULL,'later','later')",(f'{index:032x}',))
        store.db.commit()
    app=create_app(root,start_worker=False)
    response=call(app,'GET','/api/queue?filter=indexing').json()
    assert response['total']==0 and response['stage_counts']['history']==1
    assert call(app,'GET',f"/api/candidate?id={first['id']}").json()['review']['id']==manifest['batch_id']


def test_stage_search_and_pagination_do_not_cap_queue_or_change_global_counts(tmp_path):
    root=tmp_path/'state'
    for index in range(61):
        row=add_candidate(root,url=f'http://guild-{index:02}.example/')
        with connect(root) as store:
            store.db.execute("UPDATE candidates SET state='approved_waiting_batch',decision=? WHERE id=?",(json.dumps({'reviewed_at':f'2026-01-01T00:{index:02}:00Z'}),row['id']));store.db.commit()
    app=create_app(root,start_worker=False)
    first=call(app,'GET','/api/queue?filter=queued').json()
    second=call(app,'GET','/api/queue?filter=queued&offset=50').json()
    assert first['total']==second['total']==61 and len(second['candidates'])==11
    found=call(app,'GET','/api/queue?filter=queued&search=GUILD-60').json()
    assert found['total']==1 and found['stage_counts']['queued']==61
    candidate=call(app,'GET',f"/api/candidate?id={found['candidates'][0]['id']}").json()
    assert candidate['queue_position']==61
    assert call(app,'GET','/api/queue?filter=queued&search='+'x'*201).status_code==409


@pytest.mark.parametrize('state',['deferred','rejected'])
def test_restore_requires_current_decision_and_never_approves_or_starts_work(candidate,state):
    root,row=candidate
    app=create_app(root,start_worker=False)
    decision='defer' if state=='deferred' else 'reject'
    assert call(app,'POST','/api/decisions',[{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':decision}]).status_code==200
    payload={'id':row['id'],'manifest_sha256':row['manifest_sha256']}
    assert call(app,'POST','/api/restore',{**payload,'manifest_sha256':'stale'}).status_code==409
    assert call(app,'POST','/api/restore',payload).status_code==200
    current=call(app,'GET',f"/api/candidate?id={row['id']}").json()['candidate']
    assert current['stage']=='candidates' and current['decision'] is None
    assert call(app,'POST','/api/restore',payload).status_code==409
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
        assert store.db.execute('SELECT action FROM events ORDER BY rowid DESC LIMIT 1').fetchone()[0]=='candidate_restored'


@pytest.mark.parametrize('state',['indexed','already_archived','duplicate_candidate','capturing','approved_waiting_batch'])
def test_restore_cannot_reactivate_work_or_known_archived_sites(candidate,state):
    root,row=candidate
    with connect(root) as store:
        store.db.execute('UPDATE candidates SET state=? WHERE id=?',(state,row['id']));store.db.commit()
    app=create_app(root,start_worker=False)
    assert call(app,'POST','/api/restore',{'id':row['id'],'manifest_sha256':row['manifest_sha256']}).status_code==409
    assert call(app,'GET','/api/candidate?id=unknown').status_code==409
    assert call(app,'POST','/api/restore',{'id':row['id']}).status_code==409
    assert call(app,'GET',f"/api/candidate?id={row['id']}",peer='203.0.113.1').status_code==403


def test_detail_returns_only_the_selected_capture_operation_and_queue_blocker(candidate):
    root,row=candidate
    app=create_app(root,start_worker=False)
    decision={'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'}
    call(app,'POST','/api/decisions',[decision])
    result=call(app,'POST','/api/capture',{'ids':[row['id']]}).json()
    with connect(root) as store:
        store.db.execute("UPDATE operations SET state='interrupted',error='Capture paused'");store.db.commit()
    details=call(app,'GET',f"/api/candidate?id={row['id']}").json()
    assert details['candidate']['stage']=='capturing'
    assert details['capture_operation']['id']==result['operation']
    assert details['queue_blocker']['state']=='interrupted'
    assert details['review'] is None
