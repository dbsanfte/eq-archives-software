from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest

from common import CrawlError, Store, now
from daily_budget import DailyBudget, DailyBudgetPause, AutomaticStopped, reserve_attempt, settle_attempt, require_automatic
from indexer.capture_enrichment import Enricher, AUTO_POLICY, DEFAULT_POLICY, policy, policy_for_sites
from test_enrichment import response, SOURCE


def settings(account, **changes):
    current = account.settings()
    return {key: changes.get(key, current[key]) for key in ('enabled', 'daily_usd', 'min_grade', 'grading_criteria', 'revision')}


def enabled(tmp_path, **changes):
    account = DailyBudget(tmp_path / 'luna-budget')
    account.configure(settings(account, enabled=True, **changes))
    return account


def test_concurrent_requests_cannot_overspend_and_utc_rollover_keeps_old_reservations(tmp_path, monkeypatch):
    today = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
    monkeypatch.setattr('daily_budget.instant', lambda: today)
    account = enabled(tmp_path, daily_usd=1)
    def request(_):
        try: return DailyBudget(account.root).reserve(.4)
        except DailyBudgetPause: return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = [value for value in pool.map(request, range(8)) if value]
    assert len(receipts) == 2
    assert account.snapshot()['unresolved_usd'] == .8
    account.settle(receipts[0], .1)
    account.settle(receipts[0], .1)  # Same cache receipt cannot double-count.
    with pytest.raises(CrawlError): account.settle(receipts[0], .09)
    with pytest.raises(CrawlError): account.settle('missing', 0)
    assert account.snapshot()['remaining_usd'] == .5
    receipt = account.reserve(.5)
    with pytest.raises(DailyBudgetPause) as error: account.reserve(.000000001)
    assert error.value.retry_at == '2026-10-10T00:00:00+00:00'
    today += timedelta(minutes=2)
    assert DailyBudget(account.root).snapshot()['remaining_usd'] == 1
    account.reserve(1)
    # A late response belongs to its request's day, not tomorrow's fresh budget.
    account.settle(receipt, .01)
    assert account.snapshot()['remaining_usd'] == 0


@pytest.mark.parametrize('change', [{'enabled': 1}, {'min_grade': True}, {'min_grade': 4}, {'daily_usd': 0},
    {'daily_usd': -1}, {'daily_usd': float('nan')}, {'daily_usd': '2'}, {'revision': '0'},
    {'grading_criteria': 'x'*1001}, {'grading_criteria': []}])
def test_invalid_or_stale_settings_do_not_enable_spending(tmp_path, change):
    account = DailyBudget(tmp_path / 'daily')
    with pytest.raises(CrawlError): account.configure(settings(account, **change))
    assert account.settings()['configured'] is False
    saved = settings(account, enabled=True)
    account.configure(saved)
    with pytest.raises(CrawlError, match='another session'): account.configure(saved)


def test_disable_stops_new_automatic_requests_but_retains_cap_for_approved_work(tmp_path):
    account = enabled(tmp_path, daily_usd=.5)
    with account.bind(automatic=True):
        require_automatic()
        account.configure(settings(account, enabled=False))
        with pytest.raises(AutomaticStopped): require_automatic()
        with pytest.raises(AutomaticStopped): account.reserve(.1)
    with account.bind():
        account.reserve(.5)  # Approved enrichment continues under the daily cap.
        with pytest.raises(DailyBudgetPause): account.reserve(.001)


def test_reservations_are_durable_before_local_send_and_denial_does_not_use_retry_slot(tmp_path):
    account = enabled(tmp_path, daily_usd=.1)
    with closing(Store(tmp_path / 'operation')) as store, account.bind():
        store.db.execute('BEGIN IMMEDIATE')
        attempt = store.db.execute("INSERT INTO attempts(reserved,status) VALUES (.2,'reserved')").lastrowid
        with pytest.raises(DailyBudgetPause): reserve_attempt(store, attempt, .2)
        assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
        attempt = store.db.execute("INSERT INTO attempts(reserved,status) VALUES (.1,'reserved')").lastrowid
        reserve_attempt(store, attempt, .1)
        store.db.commit()
        assert account.snapshot()['unresolved_usd'] == .1
        settle_attempt(store, attempt, 0, {})
        assert account.snapshot()['unresolved_usd'] == .1
        settle_attempt(store, attempt, .01, {'input_tokens': 5, 'output_tokens': 1})
        assert account.snapshot()['estimated_usd'] == .01
        # A lost local write retains the already-committed global reservation.
        store.db.execute('BEGIN IMMEDIATE')
        attempt = store.db.execute("INSERT INTO attempts(reserved,status) VALUES (.09,'reserved')").lastrowid
        reserve_attempt(store, attempt, .09)
        store.db.rollback()
        assert account.snapshot()['remaining_usd'] == 0


def test_legacy_receipts_are_imported_once_and_unknown_usage_stays_reserved(tmp_path):
    account = enabled(tmp_path)
    account.import_legacy('old', .5, None, now())
    account.import_legacy('old', .5, None, now())
    assert account.snapshot()['unresolved_usd'] == .5
    account.import_legacy('old', .5, .02, now())
    assert account.snapshot()['estimated_usd'] == .02
    receipt = account.reserve(.1)
    account.import_legacy('new', .1, None, now(), receipt)
    assert account.snapshot()['unresolved_usd'] == .1
    with pytest.raises(CrawlError, match='unavailable'): account.import_legacy('lost', .1, None, now(), 'absent')


def test_auto_policy_requires_original_approval_and_configured_daily_ledger(tmp_path):
    site = {'decision': {'automatic': {'enrichment_budget': 'daily-v1'}}}
    assert policy_for_sites([site]) == AUTO_POLICY
    assert policy({'indexing': AUTO_POLICY, 'sites': [site]}) == AUTO_POLICY
    assert policy({}) == DEFAULT_POLICY
    with pytest.raises(CrawlError): policy({'indexing': AUTO_POLICY, 'sites': [{}]})
    with pytest.raises(CrawlError): policy_for_sites([site, {}])
    with pytest.raises(CrawlError): Enricher(tmp_path/'unsafe', 'dummy', maximum=None)
    account = enabled(tmp_path)
    with account.bind():
        client = Enricher(tmp_path/'safe', 'dummy', maximum=None)
        client.close()


def test_auto_enrichment_continues_next_day_reuses_cache_and_keeps_manual_site_cap(candidate, tmp_path, monkeypatch):
    _, row = candidate
    capture = {**row['captures'][0], 'archive_path': 'websites/guild.example/20000101000000/eq/news.html'}
    today = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
    monkeypatch.setattr('daily_budget.instant', lambda: today)
    account = enabled(tmp_path)
    calls = []
    class Luna:
        def __init__(self, _): pass
        def request(self, payload): calls.append(payload); return response()
        def close(self): pass
    directory = tmp_path / 'enrichment'
    with account.bind():
        client = Enricher(directory, 'dummy', maximum=None, client_factory=Luna)
        # More than $2 lifetime spend must not prevent a daily-funded automatic site.
        client.store.db.execute("INSERT INTO attempts(reserved,status) VALUES (3,'reserved')")
        client.store.db.commit()
        paid = client.enrich(capture, SOURCE)
        account.reserve(account.snapshot()['remaining_usd'])
        assert client.enrich(capture, SOURCE) == paid
        with pytest.raises(DailyBudgetPause): client.enrich(capture, SOURCE + ' Second page.')
        assert len(calls) == 1
        assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 2
        client.close()
        today += timedelta(minutes=2)
        client = Enricher(directory, 'dummy', maximum=None, client_factory=Luna)
        assert client.enrich(capture, SOURCE + ' Second page.')
        assert len(calls) == 2
        client.close()
        manual = Enricher(directory, 'dummy', client_factory=Luna)
        with pytest.raises(CrawlError, match='dollar budget'): manual.enrich(capture, SOURCE + ' Third page.')
        manual.close()


def test_copied_grade_cache_cannot_settle_another_requests_reused_attempt_id(candidate, tmp_path):
    from crawler import parser
    from grading import grade
    root, row = candidate
    account = enabled(tmp_path)
    calls = []
    class Luna:
        def request(self, payload):
            calls.append(payload)
            rating = {'grade': 3, 'category': 'guild', 'confidence': 'high', 'reason': 'EQ guild',
                      'evidence': [{'slot': 0, 'excerpt': 'EverQuest guild history.'}]}
            return {'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 100},
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(rating)}]}]}
    args = parser().parse_args(['--work-dir', str(root), 'grade', '--api-key-file', 'dummy', '--max-candidates', '1', '--max-usd', '2'])
    with closing(Store(root)) as store, account.bind():
        store.db.execute('UPDATE candidates SET rating=NULL'); store.db.commit()
        grade(args, store, client=Luna())
        paid = account.snapshot()['estimated_usd']
        store.db.execute('DELETE FROM attempts')
        store.db.execute("DELETE FROM meta WHERE key LIKE 'luna_grade:%'")
        store.db.execute('UPDATE candidates SET rating=NULL')
        attempt = store.db.execute("INSERT INTO attempts(id,signature,reserved,status) VALUES (1,'unrelated-source',.1,'reserved')").lastrowid
        reserve_attempt(store, attempt, .1); store.db.commit()
        grade(args, store, client=Luna())
        assert len(calls) == 1 and account.snapshot()['estimated_usd'] == paid
        assert account.snapshot()['unresolved_usd'] == .1
        saved = store.db.execute('SELECT status,actual FROM attempts').fetchone()
        assert saved['status'] == 'reserved' and saved['actual'] is None
