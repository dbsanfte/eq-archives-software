"""Parent context is optional, source-verified, bounded and never a scope grant."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from common import CrawlError, digest
from conftest import add_candidate
from grading import sources
from review import record
from selection_samples import context
from state import connect
from wayback_transport import TransportUnavailable


def run_context(root,row,*,listing=None,body=b'<p>An EverQuest guild on Fennin Ro.</p>',error=None,returned=None):
    calls=[]
    def request(job):
        calls.append(job)
        if error:raise error
        if job['op']=='list':
            return listing if listing is not None else {'captures':[
                {'url':'http://guild.example/','timestamp':'20000101000000','digest':'H','length':'100'}]}
        Path(job['destination']).write_bytes(body)
        return {**{'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(body),'bytes':len(body),
                   'content_type':'text/html'},**(returned or {})}
    with connect(root) as store:
        raw=store.db.execute('SELECT * FROM candidates WHERE id=?',(row['id'],)).fetchone()
        captures=json.loads(raw['captures']);context(store,raw,captures,Mock(call=request))
        updated=record(store,store.db.execute('SELECT * FROM candidates WHERE id=?',(row['id'],)).fetchone())
        result=store.get('selection_context:'+row['id'])
    return calls,updated,result


def test_homepage_context_keeps_original_candidate_and_is_available_to_grading(tmp_path):
    root=tmp_path/'state';row=add_candidate(root)
    calls,updated,result=run_context(root,row)
    assert [r['op'] for r in calls]==['list','capture']
    assert result['status']=='sampled' and result['captures']==1
    assert updated['url']==row['url'] and updated['scope']==row['scope']
    assert updated['captures'][0]==row['captures'][0]
    assert updated['captures'][1]['selection_role']=='site_home'
    with connect(root) as store:
        documents=sources(store,updated,120000)
    assert documents[1]['role']=='site_home' and 'role' not in documents[0]
    assert 'Fennin Ro' in documents[1]['complete_extracted_text']
    # Restarting the step cannot spend another set of homepage requests.
    assert run_context(root,updated)[0]==[]


def test_context_prefers_candidate_era_and_checks_later_home_for_topic_changes(tmp_path):
    root=tmp_path/'state';row=add_candidate(root)
    listing={'captures':[{'url':'http://guild.example/','timestamp':stamp,'digest':'H','length':'100'}
                          for stamp in ('19990201000000','20000101000000','20011101000000')]}
    calls,updated,result=run_context(root,row,listing=listing)
    assert [c['timestamp'] for c in calls if c['op']=='capture']==['20000101000000','20011101000000']
    assert len(updated['captures'])==3 and result['captures']==2


@pytest.mark.parametrize('listing',[{'captures':[]},{'captures':[{'url':'http://other.example/','timestamp':'20000101000000','digest':'H','length':'1'}]},
    {'captures':[{'url':'http://guild.example/','timestamp':'20090101000000','digest':'H','length':'1'}]}])
def test_missing_or_out_of_boundary_home_cannot_widen_or_discard_the_eq_subsite(tmp_path,listing):
    root=tmp_path/'state';row=add_candidate(root)
    calls,updated,result=run_context(root,row,listing=listing)
    assert updated['captures']==row['captures'] and updated['scope']==row['scope']
    assert all(c['op']=='list' for c in calls) and result['status']=='unavailable'


@pytest.mark.parametrize('returned',[{'url':'http://other.example/'},{'timestamp':'20090101000000'},
    {'sha256':'0'*64},{'bytes':1}])
def test_home_capture_identity_date_and_source_bytes_are_verified(tmp_path,returned):
    root=tmp_path/'state';row=add_candidate(root)
    _,updated,result=run_context(root,row,returned=returned)
    assert updated['captures']==row['captures'] and result['status']=='unavailable'


def test_optional_context_never_crops_or_exceeds_complete_source_limit(tmp_path):
    root=tmp_path/'state';row=add_candidate(root)
    _,updated,result=run_context(root,row,body=b'<p>'+b'x'*120001+b'</p>')
    assert updated['captures']==row['captures'] and result['status']=='unavailable'


@pytest.mark.parametrize('body',[b'<script>not text</script>',b''])
def test_empty_context_is_not_evidence(tmp_path,body):
    root=tmp_path/'state';row=add_candidate(root)
    _,updated,result=run_context(root,row,body=body)
    assert updated['captures']==row['captures'] and result['status']=='unavailable'


def test_local_budget_or_missing_home_keeps_useful_primary_source(tmp_path):
    root=tmp_path/'state';row=add_candidate(root)
    _,updated,result=run_context(root,row,error=CrawlError('Cumulative Wayback budget reached'))
    assert updated['captures']==row['captures'] and 'budget' in result['reason']


def test_worker_stop_still_interrupts_optional_context(tmp_path):
    root=tmp_path/'state';row=add_candidate(root)
    with pytest.raises(TransportUnavailable):
        run_context(root,row,error=TransportUnavailable('Worker stopped'))


def test_existing_home_and_explicit_scope_do_not_trigger_extra_requests(tmp_path):
    root=tmp_path/'state';home=add_candidate(root,'http://guild.example/')
    assert run_context(root,home)[0]==[]
    row=add_candidate(root,'http://other.example/eq/news.html')
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET coverage=json_set(coverage,'$.scope_mode','custom') WHERE id=?",(row['id'],));store.db.commit()
    assert run_context(root,row)[0]==[]


def test_source_linked_eq_ancestor_gets_its_own_evidence_not_a_blind_host_expansion(tmp_path):
    root=tmp_path/'state'
    row=add_candidate(root,'http://mixed.example/everquest/guides/news.html',
                      body=b'<p>EverQuest guild history.</p><a href="../">EQ section home</a>')
    calls=[]
    def request(job):
        calls.append(job)
        if job['op']=='list':return {'captures':[{'url':job['url'],'timestamp':'20000101000000','digest':'H','length':'100'}]}
        raw=(b'<p>EverQuest guild history.</p>' if '/everquest/' in job['url'] else b'<p>Family photographs and card sales.</p>')
        Path(job['destination']).write_bytes(raw)
        return {'url':job['url'],'timestamp':job['timestamp'],'bytes':len(raw),'sha256':digest(raw),'content_type':'text/html'}
    with connect(root) as store:
        raw=store.db.execute('SELECT * FROM candidates').fetchone();captures=json.loads(raw['captures'])
        context(store,raw,captures,Mock(call=request))
        assert [r['url'] for r in calls if r['op']=='list']==['http://mixed.example/everquest/','http://mixed.example/']
        assert captures[1]['selection_scope']=='http://mixed.example/everquest/'
        assert captures[2]['selection_scope']=='http://mixed.example/'
        assert captures[1]['path']!=captures[2]['path']
        assert all(c['from']=='19990101000000' and c['to']=='20011231235959' for c in calls)


@pytest.mark.parametrize('entry', [b'<frameset><frame src="main.html"></frameset>',
    b'<a href="main.html">Click here to enter</a>'])
def test_frame_or_entry_recovery_finds_real_content_without_crossing_scope(tmp_path,entry):
    root=tmp_path/'state'
    row=add_candidate(root,'http://guild.example/',body=b'<title>EQ Guild</title>'+entry)
    calls=[]
    def request(job):
        calls.append(job)
        if job['op']=='list':return {'captures':[{'url':job['url'],'timestamp':'20000101000000','digest':'D','length':'40'}]}
        raw=b'<p>We defeated Nagafen and celebrated a guild wedding in Norrath.</p>'
        Path(job['destination']).write_bytes(raw)
        return {'url':job['url'],'timestamp':job['timestamp'],'bytes':len(raw),'sha256':digest(raw),'content_type':'text/html'}
    with connect(root) as store:
        raw=store.db.execute('SELECT * FROM candidates').fetchone();captures=json.loads(raw['captures'])
        context(store,raw,captures,Mock(call=request))
        assert [c['url'] for c in calls]==['http://guild.example/main.html']*2
        assert captures[-1]['selection_role']=='supporting_page'
        assert 'Nagafen' in sources(store,{'captures':captures},120000)[-1]['complete_extracted_text']


def test_frame_recovery_does_not_follow_another_host_or_shared_account(tmp_path):
    root=tmp_path/'state'
    row=add_candidate(root,'http://shared.example/~guild/',body=b'<title>EQ Guild</title><frame src="/~other/main.html"><iframe src="http://other.example/main.html">')
    assert run_context(root,row)[0]==[]


@pytest.mark.parametrize('complete',[False,True])
def test_outside_homepage_context_never_leaks_into_published_capture_files(tmp_path,complete):
    from captures import capture_sites,check_manifest
    from conftest import manifest_for
    root=tmp_path/'state';row=add_candidate(root)
    _,updated,_=run_context(root,row)
    site=manifest_for(updated)['sites'][0]
    if complete:site['capture_policy']='complete-files-v1'
    class EmptyArchive:
        def __init__(self,*args):pass
        def call(self,job):
            assert job['op'] in ('list','scope_list','capture_list')
            from common import within_capture_scope
            assert within_capture_scope(job['url'],site['scope'])
            return {'captures':[],'resume_key':None,'listing_limited':False,'available_rows':0,'identity_variants':[]}
        def close(self):pass
    result=capture_sites(root,'a'*32,[site],EmptyArchive)
    assert len(result['captures'])==1
    assert result['captures'][0]['url']==row['url']
    assert all(c.get('selection_role')!='site_home' for c in result['captures'])
    check_manifest(root,result)
