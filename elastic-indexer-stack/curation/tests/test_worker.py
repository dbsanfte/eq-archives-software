import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from common import CrawlError, Store, digest
from graph import staged_links
from review import checked_sources, record
from state import connect, enqueue
from worker import Worker, campaign
from conftest import add_candidate, manifest_for


def test_campaign_reuses_cached_graph_merges_artifacts_and_retains_paid_reservations(candidate,monkeypatch):
    root,known=candidate
    op={'id':'a'*32,'payload':{'max_candidates':50,'max_usd':2}}
    discovery=[]
    def discover(args,run):
        discovery.append(args.max_candidates)
        assert 'http://guild.example/' in run.get('excluded_scopes')
        add_candidate(run.root,url='http://new.example/eq/news.html',grade=None)
        run.set('discovery_result',{'candidates':1})
    monkeypatch.setattr('worker.discover',discover)
    monkeypatch.setattr('worker.sample',lambda args,run:run.set('sampling_result',{'staged':1}))
    response={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({
        'grade':3,'category':'guild','confidence':'high','reason':'EQ guild history.','evidence':[{'slot':0,'excerpt':'EverQuest guild history.'}]})}]}],
        'usage':{'input_tokens':100,'output_tokens':100}}
    with patch('grading.Luna') as client:
        client.return_value.request.return_value=response
        result=campaign(root,op)
        assert result['candidates']==1
        assert client.return_value.request.call_count==1
        campaign(root,op)
        assert client.return_value.request.call_count==1
    assert discovery==[50]
    with connect(root) as store:
        rows=store.candidates()
        assert len(rows)==2
        new=next(record(store,row) for row in rows if 'new.example' in row['url'])
        assert new['captures'][0]['path'].startswith('runs/'+op['id']+'/')
        assert new['scope']=='http://new.example/eq/'
        assert checked_sources(store,new)[0]['complete_extracted_text']
    run=Store(root/'runs'/op['id'])
    try:
        assert run.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==1
        assert run.get('luna_budget_usd')==2
    finally:run.close()


def test_graded_staging_sources_spider_outward_once_with_provenance(candidate,tmp_path):
    root,row=candidate
    run=Store(tmp_path/'run')
    try:
        # The original fixture only has an internal link; add a source-bound
        # external link to a new verified high-grade candidate.
        add_candidate(root,url='http://links.example/eq/',body=b'<p>EverQuest guild history.</p><a href="http://other.example/eq/">Guild</a>')
        result=staged_links(root,run)
        assert result['staged_source_reads']==2
        evidence=json.loads(run.db.execute("SELECT evidence FROM links WHERE url='http://other.example/eq/'").fetchone()[0])
        assert evidence['source_judgment']=='model' and evidence['source_grade']==3
        assert 'source_staging_manifest' in evidence
        assert staged_links(root,run)['staged_source_reads']==0
    finally:run.close()


def test_capture_then_publication_operation_is_durable_without_any_index_write(candidate,monkeypatch):
    root,row=candidate
    manifest=manifest_for(row)
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'capturing',NULL,NULL,NULL,NULL,NULL,'now','now')",(manifest['batch_id'],))
        store.db.commit()
        enqueue(store,'capture',{'batch_id':manifest['batch_id'],'sites':manifest['sites']})
    monkeypatch.setattr('worker.capture_sites',lambda *args:manifest)
    worker=Worker(root)
    try:
        worker.operation()
        with connect(root) as store:
            assert store.db.execute('SELECT state FROM batches').fetchone()[0]=='awaiting_review'
            assert store.db.execute('SELECT state FROM operations').fetchone()[0]=='completed'
            store.db.execute("UPDATE batches SET state='publication_requested'")
            store.db.commit()
            enqueue(store,'publish',{'batch_id':manifest['batch_id'],'manifest_sha256':digest(manifest)})
        monkeypatch.setattr('worker.publish',lambda *args:{'commit':'b'*40,'marker':'crawl-manifests/test.json'})
        worker.operation()
        with connect(root) as store:
            assert store.db.execute('SELECT state FROM batches').fetchone()[0]=='published_waiting_index'
            assert store.db.execute('SELECT state FROM candidates').fetchone()[0]=='published'
        exported=json.loads((root/'batches'/manifest['batch_id']/'approved.json').read_text())
        assert exported['manifest_sha256']==digest(manifest)
        assert exported['publication']['commit']=='b'*40
    finally:worker.lease.close()


def test_unknown_download_failure_stays_interrupted_and_sanitizes_upstream_details(candidate,monkeypatch):
    root,row=candidate
    with connect(root) as store:
        enqueue(store,'capture',{'batch_id':'a'*32,'sites':[]})
    def fail(*args):raise RuntimeError('secret-like upstream response must not appear in queue')
    monkeypatch.setattr('worker.capture_sites',fail)
    worker=Worker(root)
    try:
        worker.operation()
        with connect(root) as store:
            op=store.db.execute('SELECT state,error FROM operations').fetchone()
            assert op[0]=='interrupted'
            assert 'RuntimeError' in op[1]
            assert 'secret-like' not in op[1]
    finally:worker.lease.close()
