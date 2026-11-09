"""A good EQ subsite never licenses unrelated siblings on its parent host."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import automation
from conftest import add_candidate
from common import CrawlError, digest
from grading import (LEGACY_SIGNATURE, PROMPT, current_rating, grader_signature,
                     grade, judgment_signature, sources, validate)
from review import checked_sources, record
from selection_policy import VERSION, automatic_scope, home_source
from state import connect
from test_automation import enable


def test_mixed_parent_cannot_expand_an_eq_subdirectory(tmp_path):
    root=tmp_path/'state'
    row=add_candidate(root,'http://mixed.example/crimson/news.html')
    with connect(root) as store:
        rating={**row['rating'],'relevance_grade':3,'quality_grade':3,'site_type':'mixed',
                'scope_evidence':[],'selection_policy':'eq-site-selection-v2',
                'grader_signature':grader_signature()}
        store.db.execute('UPDATE candidates SET rating=? WHERE id=?',(json.dumps(rating),row['id']))
        store.db.commit()
    account=enable(root)
    assert automation.promote(root,account)['id']==row['id']
    with connect(root) as store:
        approved=record(store,store.db.execute('SELECT * FROM candidates WHERE id=?',(row['id'],)).fetchone())
    assert approved['scope']=='http://mixed.example/crimson/'
    assert approved['scope_mode']=='directory'


def model_result(**changes):
    return {'grade':3,'relevance_grade':3,'quality_grade':3,'content_kind':'substantive',
            'site_type':'unknown','scope_evidence':[], 'category':'guild','confidence':'high',
            'reason':'A useful contemporary EQ community record.',
            'evidence':[{'slot':0,'excerpt':'EverQuest guild history.'}], **changes}


def received(result):
    return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(result)}]}]}


def docs(url='http://mixed.example/eq/news.html',text='EverQuest guild history.'):
    return [{'slot':0,'url':url,'timestamp':'20000101000000','sha256':'a'*64,'complete_extracted_text':text}]


@pytest.mark.parametrize('kind,maximum',[('identity_only',1),('unavailable',0),('unrelated',0)])
def test_an_eq_title_or_error_cannot_receive_a_top_selection_score(kind,maximum):
    raw=received(model_result(content_kind=kind));before=copy.deepcopy(raw)
    rating=validate(raw,docs())
    assert rating['relevance_grade']==3
    assert rating['grade']==rating['quality_grade']==maximum
    assert raw==before  # The paid response is never rewritten.


def test_short_first_hand_character_data_can_still_receive_top_quality():
    text='Perkalees is a level 35 Wood Elf Druid and officer of Sacred Norrath Knights.'
    rating=validate(received(model_result(evidence=[{'slot':0,'excerpt':text}])),docs(text=text))
    assert rating['quality_grade']==rating['grade']==3
    assert 'no word-count' in PROMPT and 'off-topic forum conversations' in PROMPT


@pytest.mark.parametrize('changes',[{'relevance_grade':True},{'quality_grade':4},{'site_type':'anything'},
    {'content_kind':'anything'},{'scope_evidence':{}},{'scope_evidence':[None]},
    {'scope_evidence':[{'slot':0,'excerpt':'invented homepage proof'}]},
    {'scope_evidence':[{'slot':True,'excerpt':'EverQuest guild history.'}]}])
def test_malformed_or_invented_selection_evidence_is_rejected(changes):
    with pytest.raises(CrawlError):validate(received(model_result(**changes)),docs())


def test_a_deep_eq_page_is_not_proof_that_the_parent_site_is_eq_based():
    raw=received(model_result(site_type='dedicated_eq',scope_evidence=[{'slot':0,'excerpt':'EverQuest guild history.'}]))
    rating=validate(raw,docs())
    assert rating['site_type']=='unknown' and rating['scope_evidence']==[]
    assert rating['grade']==3  # Useful subsite content is still accepted.


@pytest.mark.parametrize('url,owner,expected',[
    ('http://guild.example/',None,True),('http://guild.example/index.html',None,True),
    ('http://guild.example/news.html',None,False),('http://guild.example/eq/index.html',None,False),
    ('https://www.guild.example/','http://guild.example/',True),
    ('http://shared.example/~eqguild/index.htm',None,True),
    ('http://shared.example/~another/','http://shared.example/~eqguild/',False),
    ('http://guild.example/index.php?guild=other',None,False),
])
def test_homepage_evidence_preserves_real_accounts_and_url_identity(url,owner,expected):
    assert home_source(url,owner) is expected


def attach_home(root,row,url='http://guild.example/',confidence='high',boundary=None):
    raw=b'<title>Guild</title><p>We are an EverQuest guild on Fennin Ro.</p><p>Members, galleries, lore and events.</p>'
    with connect(root) as store:
        path=store.root/'captures'/row['id']/'20000101000000-home.html';path.write_bytes(raw)
        home={**row['captures'][0],'url':url,'path':str(path.relative_to(store.root)),
              'sha256':digest(raw),'bytes':len(raw),'selection_role':'site_home'}
        if boundary:home['selection_scope']=boundary
        captures=row['captures']+[home]
        store.db.execute('UPDATE candidates SET captures=? WHERE id=?',(json.dumps(captures),row['id']))
        current=dict(store.db.execute('SELECT * FROM candidates WHERE id=?',(row['id'],)).fetchone())
        rating={**row['rating'],'site_type':'dedicated_eq','confidence':confidence,
                'scope_evidence':[{'slot':1,'excerpt':'We are an EverQuest guild on Fennin Ro.'}]}
        rating['signature']=judgment_signature(sources(store,current,120000))
        store.db.execute('UPDATE candidates SET rating=? WHERE id=?',(json.dumps(rating),row['id']))
        store.db.commit()


def test_verified_eq_home_expands_to_whole_site_and_keeps_children_generously(tmp_path):
    root=tmp_path/'state';row=add_candidate(root,'http://guild.example/eq/news.html')
    attach_home(root,row)
    account=enable(root)
    assert automation.promote(root,account)['id']==row['id']
    with connect(root) as store:
        approved=record(store,store.db.execute('SELECT * FROM candidates WHERE id=?',(row['id'],)).fetchone())
        assert approved['scope']=='http://guild.example/' and approved['scope_mode']=='site'
        assert approved['decision']['automatic']['selection_policy']==VERSION
        # No per-page relevance gate: social galleries outside /eq/ belong to
        # the established EQ site too.
        from common import within_capture_scope
        assert within_capture_scope('http://guild.example/galleries/wedding.html',approved['scope'])


def test_another_accounts_homepage_cannot_authorize_the_parent_host(tmp_path):
    root=tmp_path/'state';row=add_candidate(root,'http://shared.example/~guild/eq/news.html')
    attach_home(root,row,url='http://shared.example/~another/')
    account=enable(root)
    assert automation.promote(root,account)['id']==row['id']
    with connect(root) as store:
        approved=record(store,store.db.execute('SELECT * FROM candidates WHERE id=?',(row['id'],)).fetchone())
    assert approved['scope']=='http://shared.example/~guild/eq/'


def test_verified_eq_subsite_expands_within_mixed_parent_without_missing_interesting_children(tmp_path):
    root=tmp_path/'state';row=add_candidate(root,'http://guild.example/everquest/guides/fletching.html')
    attach_home(root,row,url='http://guild.example/everquest/',boundary='http://guild.example/everquest/')
    account=enable(root)
    assert automation.promote(root,account)['id']==row['id']
    with connect(root) as store:
        approved=record(store,store.db.execute('SELECT * FROM candidates WHERE id=?',(row['id'],)).fetchone())
    assert approved['scope']=='http://guild.example/everquest/'
    from common import within_capture_scope
    assert within_capture_scope('http://guild.example/everquest/social/poems.html',approved['scope'])
    assert not within_capture_scope('http://guild.example/ultima-online/index.html',approved['scope'])


def test_subsite_home_evidence_can_support_eq_extent_without_claiming_the_parent():
    documents=docs()
    documents.append({'slot':1,'url':'http://mixed.example/eq/','scope':'http://mixed.example/eq/',
                      'role':'site_home','complete_extracted_text':'The EverQuest community home.'})
    rating=validate(received(model_result(site_type='dedicated_eq',
                    scope_evidence=[{'slot':1,'excerpt':'The EverQuest community home.'}])),documents)
    assert rating['site_type']=='dedicated_eq'
    row={'url':'http://mixed.example/eq/news.html','scope':'http://mixed.example/eq/',
         'scope_mode':'directory','rating':rating}
    assert automatic_scope(row,documents)==('http://mixed.example/eq/','directory')


def test_low_confidence_is_saved_with_an_honest_reason_not_lost(tmp_path):
    root=tmp_path/'state';row=add_candidate(root)
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET rating=json_set(rating,'$.confidence','medium')")
        store.db.commit()
    account=enable(root,min_grade=3)
    assert automation.promote(root,account)['id']==row['id']
    with connect(root) as store:
        saved=record(store,store.db.execute('SELECT * FROM candidates').fetchone())
    assert saved['state']=='deferred'
    assert 'confidence' in saved['decision']['automatic']['selection_reason']
    assert saved['rating']['grade']==3 and saved['captures']==row['captures']


def test_paid_legacy_grade_remains_reviewable_but_needs_a_current_auto_assessment(tmp_path):
    root=tmp_path/'state';row=add_candidate(root)
    with connect(root) as store:
        legacy={k:row['rating'][k] for k in ('grade','category','confidence','reason','evidence')}
        legacy['signature']=digest({'grader':LEGACY_SIGNATURE,'documents':sources(store,row,120000)})
        store.db.execute('UPDATE candidates SET rating=? WHERE id=?',(json.dumps(legacy),row['id']))
        store.db.commit()
        old=record(store,store.db.execute('SELECT * FROM candidates').fetchone())
        assert checked_sources(store,old)
        assert not current_rating(old['rating']) and old['selection_current'] is False
    account=enable(root)
    assert automation.promote(root,account) is None
    automation.schedule(root,account)
    with connect(root) as store:
        operation=dict(store.db.execute('SELECT * FROM operations').fetchone())
        assert json.loads(operation['payload'])['selection_policy']==VERSION
        assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
        assert json.loads(store.db.execute('SELECT rating FROM candidates').fetchone()[0])==legacy


def test_old_signature_cannot_be_relabelled_as_a_current_quality_assessment(tmp_path):
    root=tmp_path/'state';row=add_candidate(root)
    with connect(root) as store:
        rating={**row['rating'],'signature':digest({'grader':LEGACY_SIGNATURE,'documents':sources(store,row,120000)})}
        store.db.execute('UPDATE candidates SET rating=? WHERE id=?',(json.dumps(rating),row['id']));store.db.commit()
        modified=record(store,store.db.execute('SELECT * FROM candidates').fetchone())
        with pytest.raises(CrawlError,match='stale'):checked_sources(store,modified)


def test_old_grade_is_shown_as_needing_assessment_in_filtered_candidate_views(tmp_path):
    from portal import candidate_filter
    root=tmp_path/'state';row=add_candidate(root)
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET rating=json_remove(rating,'$.selection_policy')");store.db.commit()
        old=record(store,store.db.execute('SELECT * FROM candidates').fetchone())
    assert candidate_filter([old],minimum='3')==[]
    assert candidate_filter([old],needs_grade=True)==[old]


def test_policy_upgrade_preserves_old_paid_cache_and_spends_only_once_for_new_assessment(tmp_path):
    root=tmp_path/'state';row=add_candidate(root)
    args=SimpleNamespace(max_source_characters=120000,max_candidates=1,max_usd=2,grading_criteria='',api_key_file='unused')
    client=Mock();client.request.return_value={**received(model_result()),'usage':{'input_tokens':100,'output_tokens':100}}
    with connect(root) as store:
        previous={k:row['rating'][k] for k in ('grade','category','confidence','reason','evidence')}
        signature=judgment_signature(sources(store,row,120000),legacy=True)
        previous['signature']=signature
        paid={'attempt':1,'response':received({k:previous[k] for k in ('grade','category','confidence','reason','evidence')})}
        store.db.execute('UPDATE candidates SET rating=? WHERE id=?',(json.dumps(previous),row['id']))
        store.db.execute("INSERT INTO attempts(id,candidate,signature,reserved,actual,status) VALUES (1,?,?,.1,.02,'judged')",(row['id'],signature))
        store.db.commit();store.set('luna_response:'+signature,paid);store.set('luna_grade:'+signature,previous)
    for _ in range(2):
        with connect(root) as store:
            grade(args,store,client=client)
            updated=record(store,store.db.execute('SELECT * FROM candidates').fetchone())
            assert updated['selection_current'] is True
            assert store.get('luna_response:'+signature)==paid and store.get('luna_grade:'+signature)==previous
            assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==2
            assert store.db.execute('SELECT actual FROM attempts WHERE id=1').fetchone()[0]==.02
    assert client.request.call_count==1
