import copy
import json

import pytest

from common import CrawlError
from indexer.capture_enrichment import Enricher, instructions, policy, validate


SOURCE = 'EverQuest guild raid report. Updated January 2, 2000. Final paragraph.'
RESULT = {'llm_content_flavour':'Guild Achievements','llm_summary':'An EverQuest guild raid report.',
          'llm_tags':['raid'],'llm_guessed_date':'2000-01-02',
          'llm_extracted_dates':[{'date':'2000-01-02'}],
          'date_evidence':[{'date':'2000-01-02','excerpt':'Updated January 2, 2000.'}]}


def response(result=RESULT):
    return {'status':'completed','model':'gpt-6-luna','id':'dummy-response',
            'usage':{'input_tokens':1000,'output_tokens':200},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(result)}]}]}


def test_enrichment_uses_existing_prompts_and_enums_and_reuses_paid_source_bound_result(candidate,tmp_path):
    root,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/eq/news.html'}
    calls=[]
    class FakeLuna:
        def __init__(self,*args):pass
        def request(self,payload):calls.append(payload);return response()
        def close(self):pass
    directory=tmp_path/'enrichment'
    first=Enricher(directory,'dummy-key-file',client_factory=FakeLuna)
    result=first.enrich(capture,SOURCE)
    assert result['llm_tags']==['raid'] and result['llm_model_name']=='gpt-6-luna'
    assert result['llm_guessed_date']=='2000-01-02'
    assert 'date_evidence' not in result
    first.close()
    retry=Enricher(directory,'dummy-key-file',client_factory=FakeLuna)
    try:
        assert retry.enrich(capture,SOURCE)==result
        assert len(calls)==1
        assert json.loads(calls[0]['input'])['complete_source_markdown']==SOURCE
        assert '500 characters or less' in calls[0]['instructions']
        assert 'Do not duplicate tags' in calls[0]['instructions']
        assert calls[0]['store'] is False and not calls[0].get('tools')
        assert retry.enrich(capture,SOURCE+' A new paragraph.')['llm_enrichment_signature']!=result['llm_enrichment_signature']
        assert len(calls)==2
    finally:retry.close()


def test_missing_date_remains_null_and_capture_timestamp_cannot_replace_it():
    result=copy.deepcopy(RESULT)
    result.update(llm_guessed_date=None,llm_extracted_dates=[],date_evidence=[])
    assert validate(response(result),'An undated EverQuest guild page.')['llm_guessed_date'] is None
    for change in ({'llm_guessed_date':'2000-01-01'}, {'llm_summary':'x'*501}, {'llm_tags':['raid','raid']},
                   {'llm_tags':['invented-tag']}, {'llm_content_flavour':'invented-category'}):
        with pytest.raises(CrawlError):validate(response({**result,**change}),SOURCE)
    wrong=copy.deepcopy(RESULT)
    wrong['date_evidence'][0]['excerpt']='Invented evidence.'
    with pytest.raises(CrawlError,match='source validation'):validate(response(wrong),SOURCE)
    wrong=copy.deepcopy(RESULT)
    wrong['llm_extracted_dates'][0]['date']='2000-02-31'
    with pytest.raises(CrawlError):validate(response(wrong),SOURCE)
    with pytest.raises(CrawlError,match='incomplete'):validate({'status':'incomplete'},SOURCE)


def test_date_evidence_accepts_wrapped_nbsp_headings_and_retains_verbatim_source():
    source='EverQuest news. AUGUST\n3 \u00a02000. Final paragraph.'
    result=copy.deepcopy(RESULT)
    result.update(llm_guessed_date='2000-08-03',llm_extracted_dates=[{'date':'2000-08-03'}],
                  date_evidence=[{'date':'2000-08-03','excerpt':'AUGUST 3  2000'}])
    validated=validate(response(result),source)
    assert validated['date_evidence']==[{'date':'2000-08-03','excerpt':'AUGUST\n3 \u00a02000'}]
    for excerpt in ('AUGUST 4 2000','August 3 2000','AUGUST 3 2001','   '):
        result['date_evidence'][0]['excerpt']=excerpt
        with pytest.raises(CrawlError,match='source validation'):validate(response(result),source)


def test_invalid_paid_response_is_retained_and_retry_does_not_pay_again(candidate,tmp_path):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/eq/news.html'}
    invalid=copy.deepcopy(RESULT)
    invalid['date_evidence'][0]['excerpt']='Invented evidence.'
    calls=[]
    class FakeLuna:
        def __init__(self,*args):pass
        def request(self,payload):calls.append(payload);return response(invalid)
        def close(self):pass
    directory=tmp_path/'enrichment'
    for _ in range(2):
        client=Enricher(directory,'dummy-key-file',client_factory=FakeLuna)
        try:
            with pytest.raises(CrawlError,match='source validation'):client.enrich(capture,SOURCE)
            saved=client.store.db.execute("SELECT value FROM meta WHERE key LIKE 'enrichment:%'").fetchall()
            assert len(saved)==1 and json.loads(saved[0][0])['response']==response(invalid)
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==1
        finally:client.close()
    assert len(calls)==1


def test_budget_is_reserved_before_unknown_failure_and_survives_retry(candidate,tmp_path):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/eq/news.html'}
    class Uncertain:
        def __init__(self,*args):pass
        def request(self,payload):raise CrawlError('Unknown request outcome')
        def close(self):pass
    directory=tmp_path/'enrichment'
    for expected in (1,2):
        client=Enricher(directory,'dummy-key-file',client_factory=Uncertain)
        try:
            with pytest.raises(CrawlError,match='Unknown'):client.enrich(capture,SOURCE)
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==expected
            assert client.store.db.execute('SELECT SUM(reserved) FROM attempts').fetchone()[0]>0
        finally:client.close()
    client=Enricher(directory,'dummy-key-file',client_factory=Uncertain)
    try:
        client.store.db.execute('UPDATE attempts SET reserved=1')
        client.store.db.commit()
        with pytest.raises(CrawlError,match='dollar budget'):client.enrich(capture,SOURCE)
        with pytest.raises(CrawlError,match='no text truncated'):client.enrich(capture,'x'*900001)
    finally:client.close()


def test_reviewed_policy_enables_enrichment_and_domain_prompts_keep_archive_context():
    assert policy({})=={'enrichment':True,'model':'gpt-6-luna','max_enrichment_usd':2}
    with pytest.raises(CrawlError):policy({'indexing':{'enrichment':False}})
    assert 'Castersrealm' in instructions('eq.castersrealm.com')
