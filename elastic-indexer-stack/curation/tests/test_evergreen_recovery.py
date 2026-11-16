import json
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock

from conftest import add_candidate
from server import create_app
from state import connect
from test_server import call
from test_capture_resume import interrupted
from worker import Worker


def test_pre_bounds_grades_stay_valid_without_new_payment(candidate):
    from grading import sources,grader_signature,judgment_signature,current_rating
    from review import record,checked_sources
    root,row=candidate
    with connect(root) as store:
        documents=sources(store,row,120000)
        rating={**row['rating'],'grader_signature':grader_signature(before_bounds=True),
                'signature':judgment_signature(documents,before_bounds=True)}
        store.db.execute('UPDATE candidates SET rating=? WHERE id=?',(json.dumps(rating),row['id']));store.db.commit()
        saved=record(store,store.candidates()[0])
        assert current_rating(saved['rating']) and checked_sources(store,saved)==documents


def test_cached_legacy_three_excerpts_are_revalidated_without_repaying(tmp_path,monkeypatch):
    from grading import grade,sources,judgment_signature,SCHEMA
    from crawler import parser
    from test_candidate_checks import response
    root=tmp_path/'state';row=add_candidate(root,grade=None)
    cached=response();part=cached['output'][0]['content'][0];rating=json.loads(part['text'])
    rating['evidence']=[{'slot':0,'excerpt':text} for text in ['EverQuest guild history.','EQ Guild','Guide']]
    part['text']=json.dumps(rating)
    with connect(root) as store:
        raw=store.candidates()[0];signature=judgment_signature(sources(store,raw,120000),before_bounds=True)
        attempt=store.db.execute("INSERT INTO attempts(candidate,signature,reserved,actual,status,created) VALUES (?,?,.02,.001,'response_received','now')",(row['id'],signature)).lastrowid;store.db.commit()
        store.set('luna_response:'+signature,{'attempt':attempt,'response':cached})
        client=Mock();monkeypatch.setattr('grading.Luna',Mock(return_value=client))
        args=parser().parse_args(['--work-dir',str(root),'grade','--api-key-file','unused','--max-usd','.25'])
        grade(args,store)
        result=json.loads(store.candidates()[0]['rating'])
        assert len(result['evidence'])==2 and result['signature']==signature
        assert store.get('luna_response:'+signature)['response']==cached
        assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0]==1
        client.request.assert_not_called()
    assert SCHEMA['properties']['evidence']['minItems']==1
    assert SCHEMA['properties']['evidence']['maxItems']==2
    assert SCHEMA['properties']['scope_evidence']['minItems']==0


def test_invalid_extra_excerpt_cannot_be_hidden_by_legacy_bounds_repair():
    import pytest
    from grading import validate
    from common import CrawlError
    from test_candidate_checks import response
    cached=response();part=cached['output'][0]['content'][0];rating=json.loads(part['text'])
    rating['evidence']*=2;rating['evidence'].append({'slot':0,'excerpt':'invented proof'})
    part['text']=json.dumps(rating)
    with pytest.raises(CrawlError,match='does not occur'):
        validate(cached,[{'complete_extracted_text':'EverQuest guild history.'}],repair_legacy_bounds=True)


def test_poll_decodes_only_visible_records_even_with_a_cold_history(candidate,monkeypatch):
    root,row=candidate
    with connect(root) as store:
        template=dict(store.candidates()[0]);keys=list(template)
        store.db.executemany(f"INSERT INTO candidates({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
            [[{**template,'id':f'{n:024x}','url':f'http://site-{n}.example/','state':'indexed' if n>=80 else 'approved_waiting_batch',
               'decision':json.dumps({'decision':'approve','reviewed_at':'2000'})}[k] for k in keys] for n in range(5000)])
        store.db.commit()
    app=create_app(root,start_worker=False)
    import review
    original=review.metadata;prepared=[]
    def metadata(raw):
        prepared.append(raw['id']);assert len(prepared)<=50
        return original(raw)
    monkeypatch.setattr(review,'metadata',metadata)
    def forbidden(*a,**k):raise AssertionError('Poll scanned all review manifests')
    monkeypatch.setattr('server.listing_rows',forbidden)
    result=call(app,'GET','/api/queue?compact=1&filter=queued').json()
    assert result['total']==80 and result['all_count']==5001
    assert len(prepared)==50 and result['dismissal']=={'count':1,'token':None}
    assert result['stage_counts']['history']==4920


def test_due_retry_joins_fifo_while_capture_executor_is_busy(candidate,monkeypatch):
    root,row=candidate;app=create_app(root,start_worker=False)
    identifier,payload,progress=interrupted(root,app,row)
    from capture_recovery import register
    from state import unpack
    with connect(root) as store:
        op=unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(identifier,)).fetchone())
        register(store,op,op['error'])
        store.db.execute("UPDATE capture_retries SET retry_at='2000-01-01' WHERE operation=?",(identifier,));store.db.commit()
    busy=add_candidate(root,url='http://busy.example/')
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='capturing' WHERE id=?",(busy['id'],));store.db.commit()
    worker=Worker(root);monkeypatch.setattr(worker,'indexing',Mock())
    try:
        worker.indexing_turn()  # The control loop runs independently of capture.
        with connect(root) as store:
            current=unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(identifier,)).fetchone())
            assert current['state']=='resume_queued'
            assert current['payload']==payload and current['result']==progress
            assert store.db.execute('SELECT state FROM candidates WHERE id=?',(busy['id'],)).fetchone()[0]=='capturing'
        worker.indexing_turn()
        with connect(root) as store:
            assert store.db.execute("SELECT count(*) FROM events WHERE action='automatic_capture_retry' AND candidate=?",(row['id'],)).fetchone()[0]<=1
    finally:worker.close()
