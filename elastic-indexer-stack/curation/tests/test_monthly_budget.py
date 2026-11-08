from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone

import pytest

from common import CrawlError
from daily_budget import DailyBudget,DailyBudgetPause,EnrichmentPriorityPause,current_budget,request_purpose


def configured(tmp_path,**changes):
    account=DailyBudget(tmp_path/'luna')
    account.configure({k:changes.get(k,v) for k,v in dict(enabled=True,daily_usd=20,monthly_usd=200,
                        min_grade=2,grading_criteria='',revision=0).items()})
    return account


def test_monthly_reservations_survive_day_rollover_and_concurrent_workers(tmp_path,monkeypatch):
    when=datetime(2026,11,8,23,59,tzinfo=timezone.utc)
    monkeypatch.setattr('daily_budget.instant',lambda:when)
    account=configured(tmp_path,monthly_usd=25)
    first=account.reserve(20)
    when+=timedelta(minutes=2)
    def reserve(_):
        try:return DailyBudget(account.root).reserve(2)
        except DailyBudgetPause:return None
    with ThreadPoolExecutor(max_workers=8) as pool:receipts=[r for r in pool.map(reserve,range(8)) if r]
    assert len(receipts)==2
    snapshot=account.snapshot()
    assert snapshot['monthly']['unresolved_usd']==24 and snapshot['remaining_usd']==1
    assert snapshot['daily_remaining_usd']==16 and snapshot['budget_period']=='monthly'
    account.settle(first,10)
    assert account.snapshot()['remaining_usd']==11
    account.reserve(11)
    with pytest.raises(DailyBudgetPause) as caught:account.reserve(.000000001)
    assert caught.value.period=='monthly' and caught.value.retry_at=='2026-12-01T00:00:00+00:00'


def test_monthly_reset_and_late_settlement_stay_in_original_month(tmp_path,monkeypatch):
    when=datetime(2026,11,29,23,59,tzinfo=timezone.utc)
    monkeypatch.setattr('daily_budget.instant',lambda:when)
    account=configured(tmp_path,monthly_usd=25)
    receipt=account.reserve(20)
    when+=timedelta(minutes=2);account.reserve(5)
    assert account.snapshot()['remaining_usd']==0
    when=datetime(2026,12,1,tzinfo=timezone.utc)
    assert account.snapshot()['remaining_usd']==20 and account.snapshot()['monthly']['remaining_usd']==25
    account.reserve(20);account.settle(receipt,.1)
    assert account.snapshot()['remaining_usd']==0 and account.snapshot()['monthly']['remaining_usd']==5


@pytest.mark.parametrize('value',[0,-1,True,'200',float('nan'),float('inf'),1000001])
def test_invalid_monthly_limit_cannot_change_spending(tmp_path,value):
    account=DailyBudget(tmp_path/'luna')
    with pytest.raises(CrawlError):
        account.configure(dict(enabled=True,daily_usd=20,monthly_usd=value,min_grade=2,grading_criteria='',revision=0))
    assert account.settings()['configured'] is False


def test_old_browser_settings_cannot_reset_a_saved_monthly_cap(tmp_path):
    account=configured(tmp_path,monthly_usd=50)
    account.configure(dict(enabled=True,daily_usd=10,min_grade=2,grading_criteria='',revision=1))
    assert account.settings()['monthly_usd']==50


def test_enrichment_reserves_first_and_discovery_denials_never_spend(tmp_path,monkeypatch):
    when=datetime(2026,11,8,tzinfo=timezone.utc)
    monkeypatch.setattr('daily_budget.instant',lambda:when)
    account=configured(tmp_path)
    account.prioritize('portal',True)
    with pytest.raises(EnrichmentPriorityPause):account.reserve(1)
    assert account.snapshot()['unresolved_usd']==0
    with account.bind(purpose='enrichment',owner='first-job'):
        account.reserve(1)
        when+=timedelta(seconds=290)
        account.reserve(1)  # Heartbeats the in-progress import's claim.
        when+=timedelta(seconds=20)
        assert account.snapshot()['enrichment_pending'] is True
        with account.bind():
            with pytest.raises(EnrichmentPriorityPause):account.reserve(1)
    assert not account.snapshot()['enrichment_pending']
    account.reserve(1)
    assert account.snapshot()['unresolved_usd']==3
    assert current_budget.get() is None and request_purpose.get()=='discovery'


def test_one_finished_job_cannot_release_another_jobs_priority(tmp_path):
    account=configured(tmp_path)
    account.prioritize('second-job',True)
    with account.bind(purpose='enrichment',owner='first-job'):account.reserve(1)
    with pytest.raises(EnrichmentPriorityPause):account.reserve(1)
    account.prioritize('second-job',False)
    account.reserve(1)
    with pytest.raises(ValueError):account.prioritize('',True)
    with pytest.raises(ValueError):
        with account.bind(purpose='unknown'):pass


def test_exhausted_funds_explain_monthly_reset_before_enrichment_priority(tmp_path):
    account=configured(tmp_path,monthly_usd=1)
    with account.bind(purpose='enrichment'):account.reserve(1)
    account.prioritize('portal',True)
    with pytest.raises(DailyBudgetPause) as caught:account.reserve(.01)
    assert caught.value.period=='monthly' and 'Monthly' in str(caught.value)


def test_priority_storage_errors_do_not_leak_request_context(tmp_path,monkeypatch):
    account=configured(tmp_path)
    def failure(*_):raise OSError('private filesystem failure')
    monkeypatch.setattr(account,'prioritize',failure)
    with pytest.raises(OSError):
        with account.bind(purpose='enrichment'):pass
    assert current_budget.get() is None and request_purpose.get()=='discovery'


def test_configuration_counts_earlier_month_spend_once_and_keeps_original_day(tmp_path,monkeypatch):
    import automation
    from common import Store
    from contextlib import closing
    when=datetime(2026,11,8,tzinfo=timezone.utc)
    class Clock:
        @staticmethod
        def now(_):return when
    monkeypatch.setattr('automation.datetime',Clock)
    monkeypatch.setattr('daily_budget.instant',lambda:when)
    root=tmp_path/'portal';root.mkdir()
    with closing(Store(root)) as store:
        store.db.executemany('INSERT INTO attempts(reserved,actual,status,created) VALUES (?,?,?,?)',[
            (3,2,'completed','2026-11-01T12:00:00+00:00'),
            (4,None,'reserved','2026-11-02T12:00:00+00:00'),
            (7,7,'completed','2026-10-31T12:00:00+00:00')]);store.db.commit()
    account=automation.budget(root)
    for _ in range(2):automation.seed_spend(root,account)
    account.configure(dict(enabled=True,daily_usd=20,monthly_usd=200,min_grade=2,grading_criteria='',revision=0))
    snapshot=account.snapshot()
    assert snapshot['estimated_usd']==snapshot['unresolved_usd']==0
    assert snapshot['monthly']['estimated_usd']==2 and snapshot['monthly']['unresolved_usd']==4
    assert snapshot['monthly']['remaining_usd']==194
