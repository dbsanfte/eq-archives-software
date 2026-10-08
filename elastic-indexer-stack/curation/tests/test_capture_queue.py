import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from capture_flow import Action, transition
from captures import archive_path
from common import CrawlError
from conftest import add_candidate, manifest_for
from common import digest
from review import apply_decisions
from server import create_app
from state import connect
from test_server import call
from worker import Worker


def approve(app, row):
    return call(app,'POST','/api/decisions',[{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'}])


def make_due(root):
    with connect(root) as store:
        for row in store.db.execute("SELECT id,decision FROM candidates WHERE state='approved_waiting_batch'").fetchall():
            grant=json.loads(row['decision'])
            grant['capture_after']='2020-01-01T00:00:00+00:00'
            store.db.execute('UPDATE candidates SET decision=? WHERE id=?',(json.dumps(grant),row['id']))
        store.db.commit()


def test_approval_moves_views_and_undo_restores_the_suggestion_without_execution(candidate):
    root,row=candidate
    app=create_app(root,start_worker=False)
    assert approve(app,row).status_code==200
    assert call(app,'GET','/api/queue').json()['total']==0
    queued=call(app,'GET','/api/queue?filter=approved').json()
    assert queued['total']==1 and queued['operations']==[]
    assert queued['candidates'][0]['decision']['capture_after']>queued['candidates'][0]['decision']['reviewed_at']
    payload={'id':row['id'],'manifest_sha256':row['manifest_sha256']}
    assert call(app,'POST','/api/undo',{**payload,'manifest_sha256':'stale'}).status_code==409
    assert call(app,'POST','/api/undo',payload).status_code==200
    restored=call(create_app(root,start_worker=False),'GET','/api/queue').json()['candidates'][0]
    assert restored['state']=='approval_pending' and restored['decision'] is None
    assert restored['captures']==row['captures'] and restored['rating']==row['rating']
    assert call(app,'GET','/api/queue?filter=approved').json()['total']==0
    assert call(app,'POST','/api/undo',payload).status_code==409
    with connect(root) as store:
        assert store.db.execute("SELECT COUNT(*) FROM events WHERE action='approval_undone'").fetchone()[0]==1


def test_queue_exceeds_one_page_and_drains_in_bounded_batches(tmp_path,monkeypatch):
    root=tmp_path/'state'
    rows=[add_candidate(root,url=f'http://guild{index}.example/eq/news.html') for index in range(61)]
    with connect(root) as store:
        apply_decisions(store,[{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'} for row in rows],capture_delay=60)
    app=create_app(root,start_worker=False)
    first=call(app,'GET','/api/queue?filter=approved').json()
    second=call(app,'GET','/api/queue?filter=approved&offset=50').json()
    assert first['total']==61 and len(first['candidates'])==50 and len(second['candidates'])==11
    worker=Worker(root)
    try:
        worker.capture_queue()
        assert call(app,'GET','/api/queue?filter=approved').json()['approved']==61
        make_due(root)
        worker.capture_queue()
        current=call(app,'GET','/api/queue?filter=approved').json()
        assert current['approved']==60 and current['capturing']==1
        assert len(current['operations'])==1
        claimed=current['operations'][0]['payload']['sites']
        assert [row['id'] for row in claimed]==[rows[0]['id']]
        # Later sites remain undoable even after the grace period has expired.
        assert call(app,'POST','/api/undo',{'id':rows[1]['id'],'manifest_sha256':rows[1]['manifest_sha256']}).status_code==200
        worker.capture_queue()
        assert len(call(app,'GET','/api/queue?filter=all').json()['operations'])==1
        def complete(root,batch_id,sites,progress):
            return {'schema':1,'batch_id':batch_id,'sites':sites,'captures':[
                {**site['captures'][0],'candidate_id':site['id'],'archive_path':archive_path(site['captures'][0])} for site in sites]}
        monkeypatch.setattr('worker.capture_sites',complete)
        worker.operation()
        recent=call(app,'GET','/api/queue?filter=captured').json()
        assert recent['total']==1 and all(row['coverage']['capture']['batch_id'] for row in recent['candidates'])
        assert call(app,'POST','/api/undo',{'id':rows[0]['id'],'manifest_sha256':rows[0]['manifest_sha256']}).status_code==409
        worker.capture_queue()
        assert call(app,'GET','/api/queue?filter=approved').json()['approved']==58
    finally:worker.lease.close()


def test_undo_and_worker_claim_have_one_atomic_winner(candidate):
    root,row=candidate
    app=create_app(root,start_worker=False)
    approve(app,row)
    worker=Worker(root)
    barrier=threading.Barrier(2)
    try:
        make_due(root)
        def undo():
            barrier.wait()
            return call(app,'POST','/api/undo',{'id':row['id'],'manifest_sha256':row['manifest_sha256']}).status_code
        def claim():
            barrier.wait()
            worker.capture_queue()
        with ThreadPoolExecutor(max_workers=2) as pool:
            undone=pool.submit(undo)
            captured=pool.submit(claim)
            code=undone.result()
            captured.result()
        current=call(app,'GET','/api/queue?filter=all').json()
        state=current['candidates'][0]['state']
        assert (code,state,len(current['operations'])) in ((200,'approval_pending',0),(409,'capturing',1))
    finally:worker.lease.close()


def test_legacy_approval_grace_is_persistent_across_worker_restart(candidate):
    root,row=candidate
    with connect(root) as store:
        apply_decisions(store,[{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'}])
    worker=Worker(root)
    worker.lease.close()
    app=create_app(root,start_worker=False)
    original=call(app,'GET','/api/queue?filter=approved').json()['candidates'][0]['decision']['capture_after']
    restarted=Worker(root)
    try:
        assert call(app,'GET','/api/queue?filter=approved').json()['candidates'][0]['decision']['capture_after']==original
        restarted.capture_queue()
        assert call(app,'GET','/api/queue?filter=approved').json()['operations']==[]
    finally:restarted.lease.close()


def test_publication_can_queue_during_capture_without_starting_another_download(candidate):
    from state import enqueue
    root,row=candidate
    manifest=manifest_for(row)
    with connect(root) as store:
        capture=enqueue(store,'capture',{'batch_id':'b'*32,'sites':[]})
        store.db.execute("UPDATE operations SET state='running' WHERE id=?",(capture,))
        store.db.execute("INSERT INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,'now','now')",
                         (manifest['batch_id'],json.dumps(manifest),digest(manifest)))
        store.db.commit()
    app=create_app(root,start_worker=False)
    response=call(app,'POST','/api/publish',{'id':manifest['batch_id'],'manifest_sha256':digest(manifest)})
    assert response.status_code==202
    operations=call(app,'GET','/api/queue').json()['operations']
    assert [(row['kind'],row['state']) for row in operations]==[('capture','running'),('publish','queued')]


@pytest.mark.parametrize('state,action',[('capturing',Action.UNDO),('published',Action.APPROVE),('approval_pending',Action.COMPLETE),('captured_awaiting_review',Action.INDEX)])
def test_out_of_order_transitions_are_rejected(state,action):
    with pytest.raises(CrawlError): transition(state,action)
