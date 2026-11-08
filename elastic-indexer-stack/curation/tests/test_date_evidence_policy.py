"""Optional date estimates cannot poison otherwise valid enriched documents."""
import copy
from unittest.mock import Mock

import pytest

from common import CrawlError, digest
from indexer.capture_enrichment import Enricher, validate
from test_enrichment import RESULT, SOURCE, response


def dated_result():
    return {**RESULT, 'llm_guessed_date':'2000-01-04',
            'llm_extracted_dates':[{'date':day} for day in ('2000-01-02','2000-01-03','2000-01-04')],
            'date_evidence':[{'date':'2000-01-02','excerpt':'Updated January 2, 2000.'},
                             {'date':'2000-01-03','excerpt':'January 3, 2000'}]}


DATES_SOURCE=SOURCE+' More news January 3, 2000. Final complete source paragraph.'


def test_missing_proof_keeps_supported_subset_and_clears_unsupported_guess():
    received=response(dated_result());before=copy.deepcopy(received);audit={}
    result=validate(received,DATES_SOURCE,audit=audit)
    assert result['llm_extracted_dates']==[{'date':'2000-01-02'},{'date':'2000-01-03'}]
    assert result['date_evidence']==dated_result()['date_evidence']
    assert result['llm_guessed_date'] is None
    assert result['llm_summary']==RESULT['llm_summary'] and result['llm_tags']==RESULT['llm_tags']
    assert audit['omitted_dates']==['2000-01-04']
    assert audit['supported_dates']==2 and audit['guessed_date_cleared'] is True
    assert received==before


@pytest.mark.parametrize('excerpt',['Invented source quote','updated January 2, 2000.','   ','x'*501])
def test_unmatched_empty_or_oversized_evidence_is_never_accepted(excerpt):
    value=dated_result();value['date_evidence'][0]['excerpt']=excerpt
    audit={};result=validate(response(value),DATES_SOURCE,audit=audit)
    assert result['llm_extracted_dates']==[{'date':'2000-01-03'}]
    assert result['date_evidence']==[dated_result()['date_evidence'][1]]
    assert result['llm_guessed_date'] is None
    assert set(audit['omitted_dates'])=={'2000-01-02','2000-01-04'}
    assert sum(audit['discarded_evidence'].values())==1


def test_one_bad_quote_does_not_remove_other_valid_quotes_for_the_same_day():
    value=copy.deepcopy(RESULT)
    value['date_evidence'] += [{'date':'2000-01-02','excerpt':'Invented'},
                              {'date':'2000-01-02','excerpt':'January 2, 2000.'}]
    audit={};result=validate(response(value),SOURCE,audit=audit)
    assert result['llm_extracted_dates']==RESULT['llm_extracted_dates']
    assert result['llm_guessed_date']=='2000-01-02'
    assert [item['excerpt'] for item in result['date_evidence']]==['Updated January 2, 2000.','January 2, 2000.']
    assert audit['discarded_evidence']=={'unmatched_excerpt':1}


def test_invalid_calendar_dates_and_undeclared_evidence_do_not_add_claims():
    value=dated_result()
    value['llm_extracted_dates'] += [{'date':'2000-02-31'},{'date':'unknown'}]
    value['date_evidence'] += [{'date':'2000-02-31','excerpt':'January 3, 2000'},
                              {'date':'2005-01-01','excerpt':'January 3, 2000'}]
    audit={};result=validate(response(value),DATES_SOURCE,audit=audit)
    assert result['llm_extracted_dates']==[{'date':'2000-01-02'},{'date':'2000-01-03'}]
    assert audit['invalid_date_items']==2
    assert audit['discarded_evidence']=={'invalid_date':1,'undeclared_date':1}


@pytest.mark.parametrize('guess',['2000-01-04','2000-02-31','unknown',''])
def test_optional_guess_remains_null_without_valid_declared_proof(guess):
    value={**RESULT,'llm_extracted_dates':[],'date_evidence':[],'llm_guessed_date':guess}
    audit={};result=validate(response(value),'Undated complete EverQuest guild page.',audit=audit)
    assert result['llm_guessed_date'] is None and result['llm_extracted_dates']==[]
    assert audit['guessed_date_cleared'] is True


def test_no_supported_dates_keeps_other_enrichment_instead_of_inventing_fallback():
    value=copy.deepcopy(RESULT);value['date_evidence']=[]
    result=validate(response(value),SOURCE)
    assert result['llm_extracted_dates']==result['date_evidence']==[]
    assert result['llm_guessed_date'] is None
    assert result['llm_content_flavour']==RESULT['llm_content_flavour']
    assert result['llm_summary']==RESULT['llm_summary']


@pytest.mark.parametrize('changes',[{'llm_summary':'x'*501},{'llm_tags':['bad-tag']},
    {'date_evidence':[None]},{'date_evidence':[{'date':[],'excerpt':'source'}]},
    {'llm_extracted_dates':[{'date':None}]},{'llm_guessed_date':[]},
    {'date_evidence':{}},{'llm_extracted_dates':[{'date':'2000-01-02'}]*1025}])
def test_schema_and_resource_bounds_still_require_correction(changes):
    with pytest.raises(CrawlError):validate(response({**RESULT,**changes}),SOURCE)


def test_cached_partial_evidence_is_reused_with_audited_omissions_and_no_payment(candidate,tmp_path,caplog):
    caplog.set_level('INFO',logger='indexer.capture_enrichment')
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/news.html'}
    factory=Mock(side_effect=AssertionError('Never repay for optional date cleanup'))
    directory=tmp_path/'enrichment';client=Enricher(directory,'unused',client_factory=factory)
    signature=digest({'payload':client.payload(capture,DATES_SOURCE),'raw_source_sha256':capture['sha256']})
    cached={'response':response(dated_result()),'source_sha256':capture['sha256'],'parent':'original'}
    client.store.set('enrichment:'+signature,cached);client.close()
    for _ in range(2):
        client=Enricher(directory,'unused',client_factory=factory)
        try:
            result=client.enrich(capture,DATES_SOURCE)
            assert result['llm_guessed_date'] is None and len(result['llm_extracted_dates'])==2
            assert result['llm_enrichment_signature']==signature
            receipt=client.store.get('enrichment_validation:'+signature)
            assert receipt['code']=='validated' and receipt['date_validation']['omitted_dates']==['2000-01-04']
            assert receipt['date_validation']['version']=='date-evidence-v2'
            assert client.store.get('enrichment:'+signature)==cached
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
            assert 'date_validation' not in result
        finally:client.close()
    factory.assert_not_called()
    assert 'supported=2 omitted=1 invalid=0 discarded_evidence=0 guess_cleared=True' in caplog.text
    assert 'January 3' not in caplog.text and RESULT['llm_summary'] not in caplog.text


def test_existing_paid_correction_with_more_supported_dates_is_preferred(candidate,tmp_path):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/news.html'}
    factory=Mock(side_effect=AssertionError('Use the already paid supported result'))
    client=Enricher(tmp_path/'enrichment','unused',client_factory=factory)
    try:
        signature=digest({'payload':client.payload(capture,SOURCE),'raw_source_sha256':capture['sha256']})
        correction=digest({'enrichment':signature,'correction_version':1,'correction':1})
        client.store.set('enrichment:'+signature,{'response':response({**RESULT,'date_evidence':[]})})
        client.store.set('enrichment:'+correction,{'response':response(),'parent':signature})
        result=client.enrich(capture,SOURCE)
        assert result['llm_extracted_dates']==RESULT['llm_extracted_dates']
        assert result['llm_enrichment_signature']==correction
        assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
        factory.assert_not_called()
    finally:client.close()


def test_cached_refusal_still_blocks_without_payment(candidate,tmp_path):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/news.html'}
    factory=Mock(side_effect=AssertionError('Refusal cannot start another paid request'))
    client=Enricher(tmp_path/'enrichment','unused',client_factory=factory)
    try:
        signature=digest({'payload':client.payload(capture,SOURCE),'raw_source_sha256':capture['sha256']})
        refused={**response(),'output':[{'type':'message','content':[{'type':'refusal','refusal':'Private output'}]}]}
        client.store.set('enrichment:'+signature,{'response':refused})
        with pytest.raises(CrawlError) as error:client.enrich(capture,SOURCE)
        assert 'Private output' not in str(error.value)
        assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
        factory.assert_not_called()
    finally:client.close()
