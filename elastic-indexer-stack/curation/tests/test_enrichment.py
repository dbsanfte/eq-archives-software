import copy
from contextlib import closing
import json
from unittest.mock import Mock

import pytest

from common import CrawlError, digest
from import_status import EnrichmentError
from indexer import capture_enrichment, search_enrichment
from indexer.capture_enrichment import (MAINTENANCE_SCHEMA, Enricher, adaptive_summary,
                                       instructions, policy, response_metadata, validate)


SOURCE = 'EverQuest guild raid report. Updated January 2, 2000. Final paragraph.'
RESULT = {'llm_content_flavour':'Guild Achievements','llm_summary':'An EverQuest guild raid report.',
          'llm_tags':['raid'],'llm_guessed_date':'2000-01-02',
          'llm_extracted_dates':[{'date':'2000-01-02'}],
          'date_evidence':[{'date':'2000-01-02','excerpt':'Updated January 2, 2000.'}]}


def response(result=RESULT):
    return {'status':'completed','model':'gpt-6-luna','id':'dummy-response',
            'usage':{'input_tokens':1000,'output_tokens':200},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(result)}]}]}


PROJECTION = 'Spell and equipment requirements for an EverQuest guild. ' * 120


def summary_result(length, role, *, maintenance=False):
    value = {**RESULT, 'llm_summary': 'x'*length, 'content_role': role, 'topics': [], 'entities': []}
    return {k: value[k] for k in MAINTENANCE_SCHEMA['required']} if maintenance else value


@pytest.mark.parametrize('maintenance', [False, True])
def test_adaptive_summary_repairs_a_1201_character_answer_and_accepts_1200(candidate, tmp_path, maintenance):
    _, row = candidate
    capture = {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/raid.txt'}
    factory = Mock()
    luna = factory.return_value
    luna.request.side_effect = [
        response(summary_result(n, 'discussion', maintenance=maintenance)) for n in (1201, 1200)]
    client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
    try:
        def enrich():
            if maintenance:
                return client.enrich_for_maintenance(capture, SOURCE, search_enrichment.evidence_parts(PROJECTION))
            return client.enrich(capture, SOURCE, PROJECTION)
        result = enrich()
        assert len(result['llm_summary']) == 1200
        assert result['llm_content_role'] == 'discussion'
        assert ('llm_guessed_date' in result) == (not maintenance)
        assert enrich() == result
        assert luna.request.call_count == 2
        for call in luna.request.call_args_list:
            assert 'up to 1200 characters' in call.args[0]['instructions']
        assert 'rejected_response' in json.loads(luna.request.call_args.args[0]['input'])
    finally:
        client.close()


@pytest.mark.parametrize('role', ['navigation', 'directory', 'roster', 'off-topic', 'unknown'])
def test_long_evidence_does_not_expand_utility_summaries(role):
    for length in (500, 501):
        result = response(summary_result(length, role, maintenance=True))
        if length == 500:
            assert response_metadata(result, MAINTENANCE_SCHEMA, summary_max=1200)['content_role'] == role
        else:
            with pytest.raises(EnrichmentError):
                response_metadata(result, MAINTENANCE_SCHEMA, summary_max=1200)


def test_short_and_markup_only_evidence_preserves_the_exact_prompt():
    payload = {'instructions': 'Keep the summary to 500 characters or less.'}
    assert adaptive_summary(payload, ()) is payload
    assert adaptive_summary(payload, ('# Requirements', ' - []()\n' * 1000)) is payload
    assert adaptive_summary(payload, ('あ' * 2999,)) is payload
    assert 'up to 1200 characters' in adaptive_summary(payload, ('あ' * 3000,))['instructions']


@pytest.mark.parametrize(('host', 'explicit'), [
    ('eqloot.gamez.com', True), ('eqbeastiary.allakhazam.com', True),
    ('eqforums.station.sony.com', False),
])
def test_catalog_transport_keeps_logical_enrichment_signature_and_saved_charges(tmp_path, host, explicit):
    capture = {'url': f'http://{host}/raid', 'archive_path': 'websites/site/raid', 'sha256': 'raw'}
    grounding = (SOURCE,)
    ready = response(summary_result(50, 'reference', maintenance=True))
    ready['usage']['input_tokens_details'] = {'cached_tokens': 500, 'cache_write_tokens': 100}
    factory = Mock()
    factory.return_value.request.return_value = ready
    from test_policy_enrichment import budget
    account = budget(tmp_path)
    with account.bind(purpose='maintenance'), closing(Enricher(tmp_path/'paid', 'unused', client_factory=factory)) as enricher:
        logical = enricher.maintenance_payload(capture, enricher.payload(capture, SOURCE, grounding=grounding), grounding)
        signature = digest({'payload': logical, 'raw_source_sha256': capture['sha256']})
        result = enricher.enrich_for_maintenance(capture, SOURCE, grounding)
        request = factory.return_value.request.call_args.args[0]
        assert result['llm_enrichment_signature'] == signature
        assert enricher.prefix_requests == int(explicit)
        if explicit:
            assert request['prompt_cache_options'] == {'mode': 'explicit', 'ttl': '30m'}
            developer, user = request['input']
            assert developer['role'] == 'developer'
            assert developer['content'] == [{'type': 'input_text', 'text': logical['instructions'],
                                             'prompt_cache_breakpoint': {'mode': 'explicit'}}]
            assert user == {'role': 'user', 'content': logical['input']}
        else:
            assert request == logical
        assert enricher.enrich_for_maintenance(capture, SOURCE, grounding) == result
        assert factory.return_value.request.call_count == 1
        assert enricher.store.db.execute('SELECT actual FROM attempts').fetchone()[0] == .0001575
        assert account.snapshot()['estimated_usd'] == .0001575


@pytest.mark.parametrize('maintenance', [False, True])
@pytest.mark.parametrize('prior_paid', [False, True])
def test_adaptive_summary_keeps_existing_paid_and_ambiguous_attempts(candidate, tmp_path, maintenance, prior_paid):
    _, row = candidate
    capture = {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/raid.txt'}
    factory = Mock()
    luna = factory.return_value
    standard = response(summary_result(500, 'article', maintenance=maintenance))
    luna.request.side_effect = [standard] if prior_paid else [CrawlError('Transport lost'), standard]
    client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
    try:
        evidence = search_enrichment.evidence_parts(PROJECTION)
        initial = client.payload(capture, SOURCE, grounding=evidence)
        if maintenance:
            initial = client.maintenance_payload(capture, initial, evidence)
        signature = digest({'payload':initial, 'raw_source_sha256':capture['sha256']})
        if prior_paid:
            client.exchange(capture, initial, signature)
        else:
            with pytest.raises(EnrichmentError):
                client.exchange(capture, initial, signature)
        value = (client.enrich_for_maintenance(capture, SOURCE, evidence) if maintenance else
                 client.enrich(capture, SOURCE, PROJECTION))
        assert value['llm_enrichment_signature'] == signature
        assert len(value['llm_summary']) == 500
        assert luna.request.call_count == (1 if prior_paid else 2)
        assert luna.request.call_args.args[0] == initial
    finally:
        client.close()


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


def test_new_search_labels_require_passage_evidence_and_share_the_paid_checkpoint(candidate, tmp_path):
    _, row = candidate
    capture = {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/raid.txt'}
    source = SOURCE + '\nFreeport travel index.'
    projection = ('# Guild news\n\nRaid strategy for Temple of Veeshan.\n'
                  'Our ToV event ended on Sunday.\n\n[Member news](https://somewhere.example/Luclin)')
    generated = {**RESULT, 'content_role': 'article',
                 'topics': [{'name': 'Raid strategy', 'excerpt': 'Raid   strategy for Temple of Veeshan.'},
                            {'name': 'Freeport travel', 'excerpt': 'Freeport travel index.'},
                            {'name': 'Raid rewards', 'excerpt': 'Raid strategy for Temple of Veeshan.'}],
                 'entities': [{'kind': 'zone', 'name': 'ToV', 'excerpt': 'Our ToV event ended on Sunday.'},
                              {'kind': 'zone', 'name': 'Temple of Veeshan', 'excerpt': 'Raid strategy for Temple of Veeshan.'},
                              {'kind': 'zone', 'name': 'Luclin', 'excerpt': 'https://somewhere.example/Luclin'}]}
    factory = Mock()
    luna = factory.return_value
    luna.request.return_value = response(generated)
    client = Enricher(tmp_path / 'enrichment', 'unused', client_factory=factory)
    try:
        result = client.enrich(capture, source, projection)
        assert result['llm_topics'] == ['raid strategy']
        assert result['llm_entities'] == ['zone: Temple of Veeshan']
        assert result['llm_content_role'] == 'article'
        assert result['llm_guessed_date'] == '2000-01-02'
        assert 'search_evidence' not in result
        request = luna.request.call_args.args[0]
        supplied = json.loads(request['input'])
        assert supplied['complete_source_markdown'] == source
        assert 'https://' not in supplied['search_passages'][0]
        receipt = client.store.get('enrichment_validation:' + result['llm_enrichment_signature'])
        assert receipt['search_evidence'][0]['excerpt'] == 'Raid strategy for Temple of Veeshan.'
        assert receipt['search_view_sha256'] == digest(tuple(supplied['search_passages']))
        assert client.enrich(capture, source, projection) == result
        luna.request.assert_called_once()
    finally:
        client.close()


def test_already_paid_legacy_record_needs_no_second_model_call_for_a_passage_view(candidate, tmp_path):
    _, row = candidate
    capture = {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/raid.txt'}
    factory = Mock()
    factory.return_value.request.return_value = response()
    client = Enricher(tmp_path / 'enrichment', 'unused', client_factory=factory)
    try:
        old = client.enrich(capture, SOURCE)
        assert client.enrich(capture, SOURCE, 'EverQuest guild raid report.') == old
        assert 'llm_topics' not in old
        factory.return_value.request.assert_called_once()
    finally:
        client.close()


def test_large_enrichment_counts_complete_requests_before_reserving_initial_and_correction_spend(candidate, tmp_path):
    _, row = candidate
    capture = {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/raid.txt'}
    source = SOURCE + '\nComplete source line.' * 50_000
    factory = Mock()
    luna = factory.return_value
    # Allow the exact model boundary, including the requested output allowance.
    luna.count_input_tokens.side_effect = [400_000, 1_050_000 - 32_768]
    client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
    rejected = response({**RESULT, 'llm_summary': 'x' * 501})
    luna.request.side_effect = [rejected, response()]
    try:
        assert client.enrich(capture, source)['llm_summary'] == RESULT['llm_summary']
        assert client.enrich(capture, source)['llm_summary'] == RESULT['llm_summary']
        assert [call[0] for call in luna.mock_calls] == [
            'count_input_tokens', 'request', 'count_input_tokens', 'request']
        for counted, submitted in zip(luna.count_input_tokens.call_args_list, luna.request.call_args_list, strict=True):
            assert counted.args == submitted.args
            assert json.loads(counted.args[0]['input'])['complete_source_markdown'] == source
        assert json.loads(luna.request.call_args.args[0]['input'])['rejected_response'] == rejected
        reservations = client.store.db.execute('SELECT reserved FROM attempts ORDER BY id').fetchall()
        assert [record[0] for record in reservations] == [.124576, .278884]
    finally:
        client.close()


@pytest.mark.parametrize(('copies', 'token_result', 'expected_code', 'counted'), [
    (50_000, 1_050_000 - 32_768 + 1, 'context', True),
    (50_000, CrawlError('Invalid Luna input token count'), 'context_check', True),
    (100_000, None, 'context', False),
])
def test_large_enrichment_rejection_creates_no_paid_attempt(candidate, tmp_path, copies, token_result, expected_code, counted):
    _, row = candidate
    capture = {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/raid.txt'}
    factory = Mock()
    check = factory.return_value.count_input_tokens
    if isinstance(token_result, Exception):
        check.side_effect = token_result
    else:
        check.return_value = token_result
    client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
    try:
        with pytest.raises(EnrichmentError) as result:
            client.enrich(capture, SOURCE + '\nComplete source line.' * copies)
        assert result.value.code == expected_code
        assert check.called is counted
        factory.return_value.request.assert_not_called()
        assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
    finally:
        client.close()


def test_missing_date_remains_null_and_capture_timestamp_cannot_replace_it():
    result=copy.deepcopy(RESULT)
    result.update(llm_guessed_date=None,llm_extracted_dates=[],date_evidence=[])
    assert validate(response(result),'An undated EverQuest guild page.')['llm_guessed_date'] is None
    assert validate(response({**result,'llm_guessed_date':'2000-01-01'}),SOURCE)['llm_guessed_date'] is None
    for change in ({'llm_summary':'x'*501}, {'llm_tags':['raid','raid']},
                   {'llm_tags':['invented-tag']}, {'llm_content_flavour':'invented-category'}):
        with pytest.raises(CrawlError):validate(response({**result,**change}),SOURCE)
    wrong=copy.deepcopy(RESULT)
    wrong['date_evidence'][0]['excerpt']='Invented evidence.'
    assert validate(response(wrong),SOURCE)['llm_extracted_dates']==[]
    wrong=copy.deepcopy(RESULT)
    wrong['llm_extracted_dates'][0]['date']='2000-02-31'
    assert validate(response(wrong),SOURCE)['llm_guessed_date'] is None
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
        assert validate(response(result),source)['date_evidence']==[]


def test_invalid_paid_responses_stop_after_two_corrections_across_restarts(candidate,tmp_path):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/eq/news.html'}
    invalid=copy.deepcopy(RESULT)
    invalid['llm_summary']='x'*501
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
            assert len(saved)==3 and all(json.loads(row[0])['response']==response(invalid) for row in saved)
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==3
        finally:client.close()
    assert len(calls)==3


def test_budget_is_reserved_before_unknown_failure_and_survives_retry(candidate,tmp_path):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/eq/news.html'}
    class Uncertain:
        def __init__(self,*args):pass
        def request(self,payload):raise CrawlError('Unknown request outcome')
        def close(self):pass
    directory=tmp_path/'enrichment'
    for expected in (1,2):
        client=Enricher(directory,'dummy-key-file',maximum=2,client_factory=Uncertain)
        try:
            with pytest.raises(CrawlError,match='outcome is uncertain'):client.enrich(capture,SOURCE)
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==expected
            assert client.store.db.execute('SELECT SUM(reserved) FROM attempts').fetchone()[0]>0
        finally:client.close()
    client=Enricher(directory,'dummy-key-file',maximum=2,client_factory=Uncertain)
    try:
        with pytest.raises(CrawlError,match='outcome is uncertain'):client.enrich(capture,SOURCE)
        assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==2
        client.store.db.execute('UPDATE attempts SET reserved=1')
        client.store.db.commit()
        with pytest.raises(CrawlError,match='dollar budget'):client.enrich(capture,SOURCE+' New source.')
        with pytest.raises(EnrichmentError) as error:
            client.enrich(capture, 'x' * 2_000_001)
        assert error.value.code == 'context'
    finally:client.close()


def test_reviewed_policy_enables_enrichment_and_domain_prompts_keep_archive_context():
    assert policy({})=={'enrichment':True,'model':'gpt-6-luna','max_enrichment_usd':2}
    with pytest.raises(CrawlError):policy({'indexing':{'enrichment':False}})
    assert 'Castersrealm' in instructions('eq.castersrealm.com')


def test_prompt_yaml_is_loaded_once_with_original_override_order_and_independent_results(tmp_path, monkeypatch):
    templates = tmp_path / 'prompts'
    templates.mkdir()
    (templates / 'default.yml').write_text('text_prompts: {summary: Default}')
    (templates / 'a-specific.yml').write_text('domain_match_regex: raid.example\ntext_prompts: {summary: Raid}')
    (templates / 'b-general.yml').write_text('domain_match_regex: example\ntext_prompts: {summary: General}')
    monkeypatch.setattr(capture_enrichment, 'RESOURCES', tmp_path)
    load = capture_enrichment.yaml.safe_load
    calls = []

    def counted(value):
        calls.append(value)
        return load(value)

    monkeypatch.setattr(capture_enrichment.yaml, 'safe_load', counted)
    chosen = capture_enrichment.domain_prompts('raid.example')
    assert chosen == {'summary': 'Raid'}
    chosen['summary'] = 'Changed by the caller'
    assert capture_enrichment.domain_prompts('raid.example') == {'summary': 'Raid'}
    assert capture_enrichment.domain_prompts('other.example') == {'summary': 'General'}
    assert capture_enrichment.domain_prompts('unrelated.test') == {'summary': 'Default'}
    assert len(calls) == 3


def table_result(excerpt='Last Comment | 10/13/99 2:54:01 am'):
    return {**RESULT, 'llm_guessed_date':'1999-10-13', 'llm_extracted_dates':[{'date':'1999-10-13'}],
            'date_evidence':[{'date':'1999-10-13','excerpt':excerpt}]}


TABLE_SOURCE='| Forum | Last Comment |\n| --- | --- |\n| General | 10/13/99 2:54:01 am |'


def test_table_evidence_recovers_with_verbatim_cells_and_reuses_corrected_response(candidate,tmp_path):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/index.html'}
    calls=[]
    bad=response({**table_result(),'llm_summary':'x'*501});good=response(table_result('10/13/99 2:54:01 am'))
    class FakeLuna:
        def __init__(self,*args):pass
        def request(self,payload):
            calls.append(payload)
            return copy.deepcopy(bad if len(calls)==1 else good)
        def close(self):pass
    for restart in range(2):
        client=Enricher(tmp_path/'enrichment','dummy',client_factory=FakeLuna)
        try:
            result=client.enrich(capture,TABLE_SOURCE)
            assert result['llm_guessed_date']=='1999-10-13'
            assert len(calls)==2
            records=[json.loads(row[0]) for row in client.store.db.execute("SELECT value FROM meta WHERE key LIKE 'enrichment:%'")]
            assert len(records)==2
            original=next(row for row in records if row['response']==bad)
            corrected=next(row for row in records if row['response']==good)
            assert corrected['parent']==original['signature']
            assert result['llm_enrichment_signature']==corrected['signature']
            evidence=client.store.get('enrichment_validation:'+corrected['signature'])
            assert evidence['date_evidence']==[{'date':'1999-10-13','excerpt':'10/13/99 2:54:01 am'}]
            repair=json.loads(calls[1]['input'])
            assert repair['complete_source_markdown']==TABLE_SOURCE and repair['rejected_response']==bad
            assert 'invalid metadata' in calls[1]['instructions']
            assert 'never combine' in calls[0]['instructions']
        finally:client.close()


@pytest.mark.parametrize('valid',[True,False])
def test_legacy_paid_cache_is_reused_without_repeating_original_request(candidate,tmp_path,valid):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/index.html'}
    calls=[]
    class FakeLuna:
        def __init__(self,*args):pass
        def request(self,payload):calls.append(payload);return response(table_result('10/13/99 2:54:01 am'))
        def close(self):pass
    client=Enricher(tmp_path/'enrichment','dummy',client_factory=FakeLuna)
    try:
        old=client.payload(capture,TABLE_SOURCE,legacy=True)
        signature=digest({'payload':old,'raw_source_sha256':capture['sha256']})
        cached={'response':response(table_result('10/13/99 2:54:01 am') if valid else {**table_result(),'llm_summary':'x'*501}),
                'signature':signature,'source_sha256':capture['sha256'],'received_at':'original'}
        client.store.set('enrichment:'+signature,cached)
        result=client.enrich(capture,TABLE_SOURCE)
        assert result['llm_guessed_date']=='1999-10-13'
        assert len(calls)==(0 if valid else 1)
        assert client.store.get('enrichment:'+signature)==cached
        assert client.enrich(capture,TABLE_SOURCE)==result
        assert len(calls)==(0 if valid else 1)
    finally:client.close()


@pytest.mark.parametrize('bad',[None,{'status':'incomplete'},response({**RESULT,'llm_summary':'x'*501})])
def test_malformed_incomplete_and_invalid_metadata_are_corrected_under_same_validation(candidate,tmp_path,bad):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/index.html'}
    calls=[]
    class FakeLuna:
        def __init__(self,*args):pass
        def request(self,payload):calls.append(payload);return bad if len(calls)==1 else response()
        def close(self):pass
    client=Enricher(tmp_path/'enrichment','dummy',client_factory=FakeLuna)
    try:
        assert client.enrich(capture,SOURCE)['llm_summary']==RESULT['llm_summary']
        assert len(calls)==2
    finally:client.close()


def test_corrections_cannot_bypass_site_budget_or_spend_again_after_ambiguous_failures(candidate,tmp_path):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/index.html'}
    calls=[]
    class Uncertain:
        def __init__(self,*args):pass
        def request(self,payload):calls.append(payload);raise CrawlError('Private upstream data')
        def close(self):pass
    directory=tmp_path/'enrichment'
    client=Enricher(directory,'dummy',maximum=2,client_factory=Uncertain)
    try:
        signature=digest({'payload':client.payload(capture,TABLE_SOURCE),'raw_source_sha256':capture['sha256']})
        client.store.set('enrichment:'+signature,{'response':response({**table_result(),'llm_summary':'x'*501})})
        client.store.db.execute("INSERT INTO attempts(candidate,signature,reserved,status) VALUES ('previous','other',2,'reserved')")
        client.store.db.commit()
        with pytest.raises(CrawlError,match='dollar budget'):client.enrich(capture,TABLE_SOURCE)
        assert not calls
        client.store.db.execute('DELETE FROM attempts')  # Isolated fixture only.
        client.store.db.commit()
    finally:client.close()
    for attempt in range(4):
        client=Enricher(directory,'dummy',maximum=2,client_factory=Uncertain)
        try:
            with pytest.raises(CrawlError) as error:client.enrich(capture,TABLE_SOURCE)
            assert 'Private upstream' not in str(error.value)
            assert len(calls)==min(attempt+1,2)
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==min(attempt+1,2)
            if attempt>=2:assert 'two correction attempts' in str(error.value)
        finally:client.close()


def test_refusal_is_preserved_without_correction_or_fallback(candidate,tmp_path):
    _,row=candidate
    capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/index.html'}
    calls=[]
    class Refused:
        def __init__(self,*args):pass
        def request(self,payload):
            calls.append(payload)
            return {'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'Private text'}]}]}
        def close(self):pass
    client=Enricher(tmp_path/'enrichment','dummy',client_factory=Refused)
    try:
        for _ in range(2):
            with pytest.raises(CrawlError,match='declined enrichment'):client.enrich(capture,SOURCE)
        assert len(calls)==1
    finally:client.close()
