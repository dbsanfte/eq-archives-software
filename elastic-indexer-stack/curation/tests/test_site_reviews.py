import json

import pytest

from common import digest
from captures import archive_path
from conftest import add_candidate, manifest_for
from server import create_app
from site_reviews import migrate
from state import connect, unpack
from test_server import call


def mixed_sites(root, state='awaiting_review'):
    rows = [add_candidate(root,url=f'http://{name}.example/eq/news.html') for name in ('guild','class')]
    parts = [manifest_for(row) for row in rows]
    original=parts[0]['captures'][0]
    raw=b'<title>Child guide</title><p>EverQuest child page.</p>'
    path=root/'captures'/rows[0]['id']/'child.html'
    path.write_bytes(raw)
    child={**original,'url':'http://guild.example/eq/guide.html','path':str(path.relative_to(root)),
           'sha256':digest(raw),'bytes':len(raw),'title':'Child guide'}
    child['archive_path']=archive_path(child)
    parts[0]['captures'].append(child)
    manifest = {**parts[0],'sites':[part['sites'][0] for part in parts],
                'captures':[capture for part in parts for capture in part['captures']]}
    with connect(root) as store:
        store.db.execute('INSERT INTO batches VALUES (?,?,?,?,NULL,NULL,NULL,?,?)',
                         (manifest['batch_id'],state,json.dumps(manifest),digest(manifest),'2026-01-01','2026-01-02'))
        for row in rows:
            coverage={'capture':{'batch_id':manifest['batch_id'],'completed_at':'2026-01-02'}}
            store.db.execute("UPDATE candidates SET state='captured_awaiting_review',coverage=? WHERE id=?",(json.dumps(coverage),row['id']))
        store.db.commit()
    return rows,manifest


def review_for(app, candidate):
    row=next(row for row in call(app,'GET','/api/queue?filter=captured').json()['candidates'] if row['id']==candidate['id'])
    return call(app,'GET','/api/site?id='+row['coverage']['capture']['review_id']).json()


def decision(app, review, value, **extra):
    return call(app,'POST','/api/site-decision',{'id':review['id'],'manifest_sha256':review['manifest_sha256'],'decision':value,**extra})


def test_mixed_ready_batches_become_independent_reviews_without_copying_sources(tmp_path):
    root=tmp_path/'state'
    rows,parent=mixed_sites(root)
    paths={capture['path']:(root/capture['path']).stat().st_mtime_ns for capture in parent['captures']}
    app=create_app(root,start_worker=False)
    reviews=[review_for(app,row) for row in rows]
    assert len({review['id'] for review in reviews})==2
    for row,review in zip(rows,reviews):
        assert [site['id'] for site in review['manifest']['sites']]==[row['id']]
        assert {capture['candidate_id'] for capture in review['manifest']['captures']}=={row['id']}
        assert review['manifest']['capture_batch_id']==parent['batch_id']
        assert review['manifest']['capture_manifest_sha256']==digest(parent)
        assert call(app,'GET',f"/api/source?batch={review['id']}&slot=0").status_code==200
    migrate(root)
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0]==3
        saved=unpack(store.db.execute('SELECT * FROM batches WHERE id=?',(parent['batch_id'],)).fetchone())
        assert saved['state']=='capture_group' and saved['manifest']==parent
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
    assert all((root/path).stat().st_mtime_ns==stamp for path,stamp in paths.items())
    assert call(app,'POST','/api/publish',{'id':parent['batch_id'],'manifest_sha256':digest(parent)}).status_code==409


def test_approve_one_site_decline_another_and_reconsider_without_cross_approval(tmp_path):
    root=tmp_path/'state'
    rows,parent=mixed_sites(root)
    app=create_app(root,start_worker=False)
    first,second=[review_for(app,row) for row in rows]
    assert decision(app,first,'approve',slots=[0]).status_code==409
    assert decision(app,first,'approve',manifest_sha256='stale').status_code==409
    assert decision(app,first,'approve').status_code==202
    assert decision(app,first,'approve').status_code==409
    assert decision(app,second,'decline').status_code==200
    assert decision(app,second,'approve').status_code==409
    with connect(root) as store:
        op=unpack(store.db.execute('SELECT * FROM operations').fetchone())
        approved=unpack(store.db.execute('SELECT * FROM batches WHERE id=?',(op['payload']['batch_id'],)).fetchone())
        assert approved['manifest']['sites'][0]['id']==rows[0]['id']
        assert approved['manifest']['captures']==first['manifest']['captures']
        assert store.db.execute('SELECT state FROM candidates WHERE id=?',(rows[1]['id'],)).fetchone()[0]=='indexing_declined'
    assert decision(app,second,'reconsider').status_code==200
    assert review_for(app,rows[1])['state']=='awaiting_review'
    assert decision(app,second,'decline').status_code==200


def test_whole_site_approval_rechecks_all_child_source_hashes(tmp_path):
    root=tmp_path/'state'
    rows,parent=mixed_sites(root)
    app=create_app(root,start_worker=False)
    review=review_for(app,rows[0])
    (root/review['manifest']['captures'][1]['path']).write_bytes(b'tampered child')
    assert decision(app,review,'approve').status_code==409
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
    assert decision(app,review,'decline').status_code==200


def test_site_review_exposes_its_own_paused_publication_and_resume_keeps_approval(tmp_path):
    root=tmp_path/'state'
    rows,parent=mixed_sites(root)
    app=create_app(root,start_worker=False)
    reviewed=review_for(app,rows[0])
    assert decision(app,reviewed,'approve').status_code==202
    with connect(root) as store:
        store.db.execute("UPDATE operations SET state='interrupted',error='SSH runtime account is missing'")
        store.db.commit()
    paused=review_for(app,rows[0])
    assert paused['operation']['state']=='interrupted'
    assert paused['operation']['error']=='SSH runtime account is missing'
    assert paused['operation']['payload']['batch_id']==paused['id']
    assert review_for(app,rows[1])['operation'] is None
    assert call(app,'POST','/api/resume',{'id':paused['operation']['id']}).status_code==202
    resumed=review_for(app,rows[0])
    assert resumed['operation']['state']=='queued' and resumed['operation']['error'] is None
    assert resumed['state']=='publication_requested' and resumed['manifest_sha256']==reviewed['manifest_sha256']


def test_empty_site_can_be_declined_but_not_approved(tmp_path):
    root=tmp_path/'state'
    rows,parent=mixed_sites(root)
    parent['captures']=[capture for capture in parent['captures'] if capture['candidate_id']==rows[0]['id']]
    with connect(root) as store:
        store.db.execute('UPDATE batches SET manifest=?,manifest_sha256=?',(json.dumps(parent),digest(parent)))
        store.db.commit()
    app=create_app(root,start_worker=False)
    review=review_for(app,rows[1])
    assert review['manifest']['captures']==[]
    assert decision(app,review,'approve').status_code==409
    assert decision(app,review,'decline').status_code==200


def test_unassociated_page_blocks_migration_without_partial_records(tmp_path):
    from common import CrawlError
    root=tmp_path/'state'
    rows,parent=mixed_sites(root)
    del parent['captures'][0]['candidate_id']
    with connect(root) as store:
        store.db.execute('UPDATE batches SET manifest=?,manifest_sha256=?',(json.dumps(parent),digest(parent)))
        store.db.commit()
    with pytest.raises(CrawlError,match='site identity'): migrate(root)
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0]==1


def test_already_approved_legacy_batches_keep_job_and_publication_identity(tmp_path):
    root=tmp_path/'state'
    rows,parent=mixed_sites(root,state='indexing')
    with connect(root) as store:
        store.db.execute("UPDATE batches SET job=?",(json.dumps({'name':'existing-import'}),))
        store.db.commit()
    app=create_app(root,start_worker=False)
    review=call(app,'GET',f"/api/site?id={parent['batch_id']}&candidate={rows[1]['id']}").json()
    assert len(review['manifest']['sites'])==1 and review['source_slots']==[2]
    assert review['job']['name']=='existing-import' and review['id']==parent['batch_id']
    assert decision(app,review,'decline').status_code==409
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0]==1


@pytest.mark.parametrize('payload',[{}, {'id':'x','manifest_sha256':'x','decision':'yes'}, []])
def test_site_decisions_require_typed_complete_payloads(tmp_path,payload):
    assert call(create_app(tmp_path/'state',start_worker=False),'POST','/api/site-decision',payload).status_code==409
