import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from common import digest
from manual import site_url
from review import checked_sources
from server import create_app
from state import connect, enqueue, unpack
from worker import Worker
from conftest import add_candidate
from test_coverage_check import archive
from test_server import call


@pytest.mark.parametrize('supplied,expected', [
    (' http://Guild.Example:80/News.html?a=1&b=%2F#top ', 'http://guild.example/News.html?a=1&b=%2F'),
    ('guild.example/eq/', 'http://guild.example/eq/'),
    ('https://guild.example/eq/', 'https://guild.example/eq/'),
    ('https://web.archive.org/web/20000102030405/http://Guild.Example/News.html?x=%2f#top', 'http://guild.example/News.html?x=%2f'),
    ('http://web.archive.org/web/2000id_/https://guild.example/a', 'https://guild.example/a'),
    ('web.archive.org/web/*/http://guild.example/', 'http://guild.example/'),
    ('https://web.archive.org/web/2001*/guild.example/eq/', 'http://guild.example/eq/'),
])
def test_manual_url_preserves_original_page_identity(supplied, expected):
    assert site_url(supplied) == expected


@pytest.mark.parametrize('url', [None, [], 3, '', ' ', 'x'*4097, 'not a url', 'javascript:alert(1)',
    'file:///etc/passwd', 'ftp://guild.example/', 'http://user:password@guild.example/',
    'http://127.0.0.1/', 'http://192.168.50.100/', 'http://localhost/', 'http://host.internal/',
    'http://[::1]/', 'http://guild.example:wrong/', 'http://guild.example/\npage',
    'https://web.archive.org/', 'https://web.archive.org/save/http://guild.example/',
    'https://web.archive.org/web/bad/http://guild.example/',
    'https://web.archive.org:8090/web/2000/http://guild.example/',
    'https://web.archive.org/web/2000/http://127.0.0.1/',
    'https://web.archive.org/web/2000/http://user:pass@guild.example/',
    'https://web.archive.org/web/2000/https://web.archive.org/'])
def test_invalid_manual_inputs_never_start_work(tmp_path, url):
    app=create_app(tmp_path/'state',start_worker=False)
    response=call(app,'POST','/api/submit-site',{'url':url,'max_usd':2})
    assert response.status_code==409
    with connect(tmp_path/'state') as store:
        assert not store.candidates()
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0


@pytest.mark.parametrize('payload', [{}, {'url':'http://guild.example/'}, {'url':'http://guild.example/','max_usd':True},
    {'url':'http://guild.example/','max_usd':0}, {'url':'http://guild.example/','max_usd':3},
    {'url':'http://guild.example/','max_usd':2,'max_candidates':50}])
def test_manual_spending_requires_explicit_single_site_bounds(tmp_path,payload):
    app=create_app(tmp_path/'state',start_worker=False)
    assert call(app,'POST','/api/submit-site',payload).status_code==409


def test_repeated_and_concurrent_submissions_share_one_budget_and_keep_provenance(tmp_path):
    root=tmp_path/'state';app=create_app(root,start_worker=False)
    url='https://web.archive.org/web/20000101000000/http://guild.example/News.html?order=2&order=1'
    def submit(_):return call(app,'POST','/api/submit-site',{'url':url,'max_usd':0.25})
    with ThreadPoolExecutor(max_workers=4) as pool:responses=list(pool.map(submit,range(4)))
    assert sorted(response.status_code for response in responses)==[200,200,200,202]
    assert len({response.json()['operation'] for response in responses})==1
    with connect(root) as store:
        op=unpack(store.db.execute('SELECT * FROM operations').fetchone())
        assert op['payload']=={'max_candidates':1,'max_usd':0.25,'grading_criteria':'','target':{
            'url':'http://guild.example/News.html?order=2&order=1','submitted_url':url}}
        store.db.execute("UPDATE operations SET state='interrupted'");store.db.commit()
    again=call(create_app(root,start_worker=False),'POST','/api/submit-site',{'url':'https://www.guild.example/other','max_usd':2,'grading_criteria':'Guild sites'})
    assert again.status_code==200 and again.json()['operation']==op['id']
    assert call(app,'POST','/api/resume',{'id':op['id']}).status_code==202
    with connect(root) as store:
        assert unpack(store.db.execute('SELECT * FROM operations').fetchone())['payload']==op['payload']


def test_existing_site_is_returned_without_changing_its_decision_or_work(candidate):
    root,row=candidate
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='rejected',decision=?", (json.dumps({'decision':'reject'}),));store.db.commit()
        enqueue(store,'publish',{'batch_id':'a'*32})
    app=create_app(root,start_worker=False)
    response=call(app,'POST','/api/submit-site',{'url':'https://www.guild.example/elsewhere.html','max_usd':2})
    assert response.status_code==200 and response.json()['candidate_id']==row['id']
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==1
        assert store.candidates()[0]['state']=='rejected'
        assert json.loads(store.candidates()[0]['decision'])=={'decision':'reject'}
    other=call(app,'POST','/api/submit-site',{'url':'http://different.example/','max_usd':2})
    assert other.status_code==202
    with connect(root) as store:
        assert store.db.execute("SELECT state FROM operations WHERE kind='publish'").fetchone()[0]=='queued'
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==2
    blocked=call(app,'POST','/api/submit-site',{'url':'http://another.example/','max_usd':2})
    assert blocked.status_code==409 and 'discovery' in blocked.json()['error']


def test_shared_accounts_remain_distinct(tmp_path):
    root=tmp_path/'state';known=add_candidate(root,url='http://geocities.com/alice/eq/')
    app=create_app(root,start_worker=False)
    existing=call(app,'POST','/api/submit-site',{'url':'https://www.geocities.com/alice/other.html','max_usd':2})
    assert existing.status_code==200 and existing.json()['candidate_id']==known['id']
    assert call(app,'POST','/api/submit-site',{'url':'http://geocities.com/bob/eq/','max_usd':2}).status_code==202


class SampleDownloader:
    requests=[]
    def __init__(self,store,args):
        assert args.max_candidates==1 and args.delay==3 and args.bytes_per_second==131072
    def call(self,request):
        self.requests.append(request)
        if request['op']=='list':
            assert request['url']=='http://new-guild.example/eq/News.html?x=1'
            assert request['from']=='19990101000000' and request['to']=='20011231235959'
            return {'captures':[{'url':request['url'],'timestamp':'20000101000000','digest':'fixture','length':100}],
                'listing_limited':False,'available_rows':1,'identity_variants':[]}
        source=b'<title>New guild</title><p>EverQuest guild history.</p>'
        Path(request['destination']).write_bytes(source)
        return {'url':request['url'],'timestamp':request['timestamp'],'sha256':digest(source),'bytes':len(source),
            'content_type':'text/html','source':'wayback'}
    def close(self):pass


def test_manual_site_gets_normal_coverage_sources_grade_and_review_without_spidering(tmp_path,monkeypatch):
    root=tmp_path/'state'
    repo=archive(tmp_path,['known.example/20000101000000/index.html']);monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    app=create_app(root,start_worker=False)
    # New campaigns must not inherit a previous campaign's phase checkpoints.
    with connect(root) as store:
        store.set('discovery_completed',True)
        store.set('sampling_completed',True)
    submitted='https://web.archive.org/web/20050304000000/http://new-guild.example/eq/News.html?x=1'
    op=call(app,'POST','/api/submit-site',{'url':submitted,'max_usd':0.2,'grading_criteria':'Guild sites'}).json()['operation']
    response={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({
        'grade':3,'category':'guild','confidence':'high','reason':'EQ guild history.',
        'evidence':[{'slot':0,'excerpt':'EverQuest guild history.'}]})}]}],'usage':{'input_tokens':100,'output_tokens':100}}
    monkeypatch.setattr('acquisition.Downloader',SampleDownloader)
    worker=Worker(root)
    try:
        with patch('worker.discover') as spider,patch('worker.staged_links') as graph,patch('grading.Luna') as client:
            client.return_value.request.return_value=response
            worker.operation()
            spider.assert_not_called();graph.assert_not_called()
            assert client.return_value.request.call_count==1
            listing=call(app,'GET','/api/queue?filter=candidates').json()
            assert listing['total']==1
            row=listing['candidates'][0]
            assert row['state']=='approval_pending' and row['rating']['grade']==3 and row['decision'] is None
            assert row['rating']['grading_criteria']=='Guild sites'
            assert json.loads(client.return_value.request.call_args.args[0]['input'])['grading_criteria']=='Guild sites'
            assert row['url']=='http://new-guild.example/eq/News.html?x=1'
            assert row['scope']=='http://new-guild.example/eq/'
            assert row['coverage']['site_check']['status']=='new_site'
            assert row['captures'][0]['tier']==1 and row['evidence'][0]['source_url']==submitted
            with connect(root) as store:
                assert checked_sources(store,row)[0]['complete_extracted_text']=='New guild\nEverQuest guild history.'
                assert unpack(store.db.execute('SELECT * FROM operations WHERE id=?',(op,)).fetchone())['result']['candidate_id']==row['id']
            # A crash after merging sources, before marking the operation complete,
            # reuses the candidate and never grades it again.
            with connect(root) as store:
                store.db.execute("UPDATE operations SET state='queued'");store.db.commit()
            worker.operation()
            assert client.return_value.request.call_count==1
    finally:worker.lease.close()


def test_already_archived_manual_site_retires_without_download_or_payment(tmp_path,monkeypatch):
    root=tmp_path/'state'
    repo=archive(tmp_path,['mythiran.com/20000101000000/index.html']);monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    app=create_app(root,start_worker=False)
    assert call(app,'POST','/api/submit-site',{'url':'https://www.mythiran.com/eq/','max_usd':2}).status_code==202
    worker=Worker(root)
    try:
        with patch('worker.sample') as download,patch('worker.grade') as paid:
            worker.operation();download.assert_not_called();paid.assert_not_called()
        listing=call(app,'GET','/api/queue?filter=history').json()
        assert listing['total']==1 and listing['candidates'][0]['state']=='already_archived'
        assert listing['operations'][0]['state']=='completed'
    finally:worker.lease.close()


def test_unverified_manual_coverage_pauses_and_resumes_without_spending(tmp_path,monkeypatch):
    from site_inventory import SiteInventory
    root=tmp_path/'state'
    repo=archive(tmp_path,['known.example/20000101000000/index.html']);monkeypatch.setenv('ARCHIVE_REPO',str(repo))
    app=create_app(root,start_worker=False)
    op=call(app,'POST','/api/submit-site',{'url':'http://new-guild.example/eq/News.html?x=1','max_usd':0.2}).json()['operation']
    worker=Worker(root)
    try:
        check=SiteInventory.check
        monkeypatch.setattr(SiteInventory,'check',lambda *args,**kwargs:{'status':'inventory_partial','complete':False,
            'progress':{'checked':2,'total':30},'message':'Resume the metadata check.'})
        with patch('worker.sample') as download,patch('worker.grade') as paid:
            worker.operation();download.assert_not_called();paid.assert_not_called()
            with connect(root) as store:
                operation=unpack(store.db.execute('SELECT * FROM operations').fetchone())
                assert operation['state']=='interrupted' and '2 of 30' in operation['error']
                assert not store.candidates()
            monkeypatch.setattr(SiteInventory,'check',check)
            assert call(app,'POST','/api/resume',{'id':op}).status_code==202
            worker.operation()
            assert download.call_count==1 and paid.call_count==1
            assert paid.call_args.args[0].max_usd==0.2
    finally:worker.lease.close()
