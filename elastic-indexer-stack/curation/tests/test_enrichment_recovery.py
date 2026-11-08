"""Recovery of valid paid date metadata without repeating inference."""
import copy
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from common import CrawlError, digest
from indexer.capture_enrichment import Enricher, validate
from test_enrichment import RESULT, SOURCE, response


@pytest.mark.parametrize('quotes', [
    ['1:09 am on May 18, 2002', '9:44 pm on May 18, 2002', '11:54 pm on May 18, 2002'],
    ['2002年05月18日', '2002年05月18日 23:30'],
])
def test_multiple_source_quotes_for_one_day_are_not_a_schema_failure(quotes):
    result = {**RESULT, 'llm_guessed_date': '2002-05-18',
              'llm_extracted_dates': [{'date': '2002-05-18'}] * 3,
              'date_evidence': [{'date': '2002-05-18', 'excerpt': quote} for quote in quotes]}
    received = response(result)
    original = copy.deepcopy(received)
    validated = validate(received, '\n'.join(quotes))
    assert validated['llm_extracted_dates'] == [{'date': '2002-05-18'}]
    assert validated['date_evidence'] == result['date_evidence']
    assert received == original


def news_result():
    days = [(date(2000, 1, 1) + timedelta(days=index)).isoformat() for index in range(128)]
    result = {**RESULT, 'llm_guessed_date': days[-1],
              'llm_extracted_dates': [{'date': day} for day in days],
              'date_evidence': [{'date': day, 'excerpt': 'News updated ' + day} for day in days]}
    return result, '\n'.join(item['excerpt'] for item in result['date_evidence'])


def test_long_news_index_keeps_every_supported_date_and_bounds_metadata():
    result, source = news_result()
    assert validate(response(result), source) == result
    repeated = copy.deepcopy(result)
    repeated['date_evidence'].append(result['date_evidence'][0])
    repeated['llm_extracted_dates'].append(result['llm_extracted_dates'][0])
    assert validate(response(repeated), source) == result
    for field in ('llm_extracted_dates', 'date_evidence'):
        excessive = {**RESULT, field: RESULT[field] * 1025}
        with pytest.raises(CrawlError):
            validate(response(excessive), SOURCE)


@pytest.mark.parametrize('change', [
    {'date_evidence': []},
    {'date_evidence': [{'date': '2000-01-03', 'excerpt': 'January 2, 2000'}]},
    {'llm_extracted_dates': [{'date': '2000-02-31'}]},
    {'date_evidence': [{'date': '2000-01-02', 'excerpt': 'Invented date'}]},
])
def test_normalization_never_accepts_missing_or_invalid_date_proof(change):
    result = validate(response({**RESULT, **change}), SOURCE)
    assert result['llm_extracted_dates'] == result['date_evidence'] == []
    assert result['llm_guessed_date'] is None


@pytest.mark.parametrize('evidence', [[None], [{'date': [], 'excerpt': 'January 2, 2000'}]])
def test_malformed_evidence_shape_still_requires_correction(evidence):
    with pytest.raises(CrawlError):
        validate(response({**RESULT, 'date_evidence': evidence}), SOURCE)


def test_previously_rejected_cached_correction_recovers_without_network(candidate, tmp_path):
    _, row = candidate
    capture = {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/news.html'}
    result, source = news_result()
    client_factory = Mock(side_effect=AssertionError('A saved paid response must be reused'))
    directory = tmp_path / 'enrichment'
    first = Enricher(directory, 'unused', client_factory=client_factory)
    signature = digest({'payload': first.payload(capture, source), 'raw_source_sha256': capture['sha256']})
    correction = digest({'enrichment': signature, 'correction_version': 1, 'correction': 1})
    records = {
        signature: {'response': response({**result, 'date_evidence': []}), 'source_sha256': capture['sha256']},
        correction: {'response': response(result), 'source_sha256': capture['sha256'], 'parent': signature},
    }
    for key, record in records.items():
        first.store.set('enrichment:' + key, record)
    first.close()
    for _ in range(2):
        client = Enricher(directory, 'unused', client_factory=client_factory)
        try:
            accepted = client.enrich(capture, source)
            assert accepted['llm_extracted_dates'] == result['llm_extracted_dates']
            assert accepted['llm_enrichment_signature'] == correction
            for key, record in records.items():
                assert client.store.get('enrichment:' + key) == record
            assert client.store.get('enrichment_validation:' + correction)['date_evidence'] == result['date_evidence']
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
        finally:
            client.close()
    client_factory.assert_not_called()


def test_settled_cost_replaces_reservation_but_uncertain_calls_still_hold_budget(candidate, tmp_path):
    _, row = candidate
    capture = {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/news.html'}
    factory = Mock()
    factory.return_value.request.return_value = response()
    directory = tmp_path / 'enrichment'
    client = Enricher(directory, 'unused', client_factory=factory)
    client.store.db.execute("INSERT INTO attempts(candidate,signature,reserved,actual,status) VALUES ('old','old',2,.1,'enriched')")
    client.store.db.commit()
    client.close()
    client = Enricher(directory, 'unused', client_factory=factory)
    try:
        assert client.enrich(capture, SOURCE)['llm_summary'] == RESULT['llm_summary']
        assert factory.return_value.request.call_count == 1
        client.store.db.execute("INSERT INTO attempts(candidate,signature,reserved,status) VALUES ('unknown','unknown',1.9,'received')")
        client.store.db.commit()
        with pytest.raises(CrawlError, match='dollar budget'):
            client.enrich(capture, SOURCE + ' Another source.')
        assert factory.return_value.request.call_count == 1
        assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 3
    finally:
        client.close()
