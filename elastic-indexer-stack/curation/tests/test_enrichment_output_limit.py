"""A larger output allowance must preserve paid caches and durable spend bounds."""
import copy
import json
from unittest.mock import Mock

import pytest

from common import CrawlError, digest
from indexer.capture_enrichment import Enricher
from test_enrichment import SOURCE, RESULT, response


def capture_for(row):
    return {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/news.html'}


def original_signature(client, capture, *, legacy=False, limit=4096):
    payload = client.payload(capture, SOURCE, legacy=legacy)
    payload['max_output_tokens'] = limit
    return digest({'payload': payload, 'raw_source_sha256': capture['sha256']})


def truncated():
    return {**response(), 'status': 'incomplete', 'max_output_tokens': 4096,
            'usage': {'input_tokens': 1000, 'output_tokens': 4096},
            'incomplete_details': {'reason': 'max_output_tokens'}}


def seed_chain(client, capture, *, legacy=False, final=None):
    original = original_signature(client, capture, legacy=legacy)
    signatures = [original] + [digest({'enrichment': original, 'correction_version': 1, 'correction': index}) for index in (1, 2)]
    records = {}
    for index, signature in enumerate(signatures):
        records[signature] = {'response': copy.deepcopy(final if index == 2 and final is not None else truncated()),
                              'signature': signature, 'source_sha256': capture['sha256'],
                              'parent': signatures[index - 1] if index else None}
        client.store.set('enrichment:' + signature, records[signature])
        client.store.db.execute("INSERT INTO attempts(candidate,signature,reserved,actual,status) VALUES (?,?,.01,.002,'received')",
                                (capture['archive_path'], signature))
        client.store.db.commit()
    return signatures, records


def test_new_requests_and_corrections_have_32k_headroom_and_reserve_it(candidate, tmp_path):
    _, row = candidate
    capture = capture_for(row)
    factory = Mock()
    factory.return_value.request.side_effect = [truncated(), response()]
    client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
    try:
        assert client.enrich(capture, SOURCE)['llm_summary'] == RESULT['llm_summary']
        calls = factory.return_value.request.call_args_list
        assert len(calls) == 2
        assert all(call.args[0]['max_output_tokens'] == 32768 for call in calls)
        reservations = client.store.db.execute('SELECT reserved FROM attempts ORDER BY id').fetchall()
        for call, record in zip(calls, reservations, strict=True):
            payload = call.args[0]
            expected = ((len(json.dumps(payload, ensure_ascii=False).encode()) + 8192) * .20 + 32768 * .75)/1_000_000
            assert record[0] == expected
    finally:
        client.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_valid_old_paid_original_and_correction_survive_limit_change(candidate, tmp_path, legacy):
    _, row = candidate
    capture = capture_for(row)
    factory = Mock(side_effect=AssertionError('Never repay a valid cached response'))
    client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
    try:
        signatures, records = seed_chain(client, capture, legacy=legacy, final=response())
        accepted = client.enrich(capture, SOURCE)
        assert accepted['llm_enrichment_signature'] == signatures[-1]
        for signature, record in records.items():
            assert client.store.get('enrichment:' + signature) == record
        # Also retain a valid original without needing either correction.
        records[signatures[0]]['response'] = response()
        client.store.set('enrichment:' + signatures[0], records[signatures[0]])
        assert client.enrich(capture, SOURCE)['llm_enrichment_signature'] == signatures[0]
        factory.assert_not_called()
    finally:
        client.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_exhausted_4k_chain_gets_one_32k_upgrade_and_reuses_it_across_jobs(candidate, tmp_path, legacy):
    _, row = candidate
    capture = capture_for(row)
    factory = Mock()
    factory.return_value.request.return_value = response()
    directory = tmp_path/'enrichment'
    client = Enricher(directory, 'unused', client_factory=factory)
    signatures, records = seed_chain(client, capture, legacy=legacy)
    client.close()
    for _ in range(2):
        client = Enricher(directory, 'unused', client_factory=factory)
        try:
            accepted = client.enrich(capture, SOURCE)
            assert accepted['llm_summary'] == RESULT['llm_summary']
            assert factory.return_value.request.call_count == 1
            assert factory.return_value.request.call_args.args[0]['max_output_tokens'] == 32768
            upgraded = client.store.get('enrichment:' + accepted['llm_enrichment_signature'])
            assert upgraded['parent'] == signatures[-1] and upgraded['max_output_tokens'] == 32768
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 4
            for signature, record in records.items():
                assert client.store.get('enrichment:' + signature) == record
        finally:
            client.close()


@pytest.mark.parametrize('outcome', [CrawlError('Private request failure'), truncated(), response({**RESULT, 'llm_tags':['bad-tag']})])
def test_failed_upgrade_never_repeats_or_resets_old_corrections(candidate, tmp_path, outcome):
    _, row = candidate
    capture = capture_for(row)
    factory = Mock()
    if isinstance(outcome, BaseException): factory.return_value.request.side_effect = outcome
    else: factory.return_value.request.return_value = outcome
    directory = tmp_path/'enrichment'
    client = Enricher(directory, 'unused', client_factory=factory)
    seed_chain(client, capture)
    client.close()
    for _ in range(3):
        client = Enricher(directory, 'unused', client_factory=factory)
        try:
            with pytest.raises(CrawlError): client.enrich(capture, SOURCE)
            assert factory.return_value.request.call_count == 1
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 4
        finally:
            client.close()


@pytest.mark.parametrize('reason', ['content_filter', 'other', None])
def test_other_exhausted_failures_do_not_gain_capacity_retry(candidate, tmp_path, reason):
    _, row = candidate
    capture = capture_for(row)
    factory = Mock(side_effect=AssertionError('Only a proven old output limit permits recovery'))
    client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
    try:
        final = truncated()
        final['incomplete_details'] = {'reason':reason}
        seed_chain(client, capture, final=final)
        with pytest.raises(CrawlError): client.enrich(capture, SOURCE)
        factory.assert_not_called()
    finally:
        client.close()


@pytest.mark.parametrize('previous_attempts', [1, 2])
def test_unknown_original_attempts_are_counted_across_the_limit_change(candidate, tmp_path, previous_attempts):
    _, row = candidate
    capture = capture_for(row)
    factory = Mock()
    factory.return_value.request.side_effect = CrawlError('Private request failure')
    client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
    try:
        signature = original_signature(client, capture)
        for _ in range(previous_attempts):
            client.store.db.execute("INSERT INTO attempts(candidate,signature,reserved,status) VALUES (?,?,.01,'reserved')", (capture['archive_path'], signature))
        client.store.db.commit()
        for _ in range(2):
            with pytest.raises(CrawlError): client.enrich(capture, SOURCE)
        assert factory.return_value.request.call_count == 2 - previous_attempts
        assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 2
        if previous_attempts == 1:
            assert factory.return_value.request.call_args.args[0]['max_output_tokens'] == 32768
    finally:
        client.close()


def test_32k_failures_and_old_slots_already_using_32k_cannot_get_another_upgrade(candidate, tmp_path):
    _, row = candidate
    capture = capture_for(row)
    factory = Mock()
    factory.return_value.request.return_value = {**truncated(), 'max_output_tokens':32768}
    directory = tmp_path/'new'
    for _ in range(2):
        client = Enricher(directory, 'unused', client_factory=factory)
        try:
            with pytest.raises(CrawlError): client.enrich(capture, SOURCE)
            assert factory.return_value.request.call_count == 3
        finally:
            client.close()
    client = Enricher(tmp_path/'old', 'unused', client_factory=factory)
    try:
        signatures, records = seed_chain(client, capture)
        client.store.set('enrichment:' + signatures[-1], {**records[signatures[-1]], 'max_output_tokens':32768})
        with pytest.raises(CrawlError): client.enrich(capture, SOURCE)
        assert factory.return_value.request.call_count == 3
    finally:
        client.close()


def test_unused_old_correction_slot_sends_32k_and_keeps_its_original_identity(candidate, tmp_path):
    _, row = candidate
    capture = capture_for(row)
    factory = Mock()
    factory.return_value.request.return_value = response()
    client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
    try:
        signature = original_signature(client, capture)
        client.store.set('enrichment:' + signature, {'response':truncated(), 'source_sha256':capture['sha256']})
        result = client.enrich(capture, SOURCE)
        assert factory.return_value.request.call_count == 1
        assert factory.return_value.request.call_args.args[0]['max_output_tokens'] == 32768
        expected = digest({'enrichment':signature, 'correction_version':1, 'correction':1})
        assert result['llm_enrichment_signature'] == expected
    finally:
        client.close()


@pytest.mark.parametrize('ceiling', ['site', 'daily'])
def test_32k_recovery_reserves_full_allowance_under_original_dollar_ceiling(candidate, tmp_path, ceiling):
    from test_daily_budget import enabled
    from daily_budget import DailyBudgetPause
    _, row = candidate
    capture = capture_for(row)
    factory = Mock(side_effect=AssertionError('Budget denial must precede any request'))
    account = enabled(tmp_path, daily_usd=.02)
    with account.bind(purpose='enrichment'):
        client = Enricher(tmp_path/'enrichment', 'unused', client_factory=factory)
        try:
            seed_chain(client, capture)
            if ceiling == 'site':
                client.store.db.execute("INSERT INTO attempts(reserved,actual,status) VALUES (2,1.98,'enriched')")
                client.store.db.commit()
            before = client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
            with pytest.raises((CrawlError, DailyBudgetPause)):
                client.enrich(capture, SOURCE)
            factory.assert_not_called()
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == before
            assert account.snapshot()['unresolved_usd'] == 0
        finally:
            client.close()
