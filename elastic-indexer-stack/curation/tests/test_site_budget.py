"""Cumulative site caps share atomic daily/monthly admission without resetting history."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime,timedelta,timezone
import copy
import json

import pytest

from common import CrawlError,Store
from daily_budget import DailyBudget,SiteBudgetExceeded,reserve_attempt,units
from indexer.capture_enrichment import Enricher,LEGACY_POLICY,LEGACY_AUTO_POLICY,DEFAULT_POLICY,AUTO_POLICY,policy
from test_enrichment import SOURCE,response


def configure(account,**changes):
    settings=account.settings()
    return account.configure({k:changes.get(k,settings.get(k)) for k in
        ('enabled','daily_usd','monthly_usd','site_usd','min_grade','grading_criteria','revision')})


def test_new_policies_cap_manual_and_automatic_sites_and_retain_legacy_manifests():
    assert DEFAULT_POLICY['max_enrichment_usd']==AUTO_POLICY['max_enrichment_usd']==5
    for original in (LEGACY_POLICY,LEGACY_AUTO_POLICY,DEFAULT_POLICY,AUTO_POLICY):
        manifest={'indexing':copy.deepcopy(original),'sites':[{'decision':{'automatic':{'enrichment_budget':'daily-v1'}}}]}
        before=copy.deepcopy(manifest)
        assert policy(manifest)==original and manifest==before
    assert policy({})==LEGACY_POLICY
    with pytest.raises(CrawlError):policy({'indexing':{**DEFAULT_POLICY,'max_enrichment_usd':99}})


@pytest.mark.parametrize('value',[0,-1,True,'5',None,float('nan'),float('inf'),1000001])
def test_invalid_site_setting_cannot_change_existing_limits(tmp_path,value):
    account=DailyBudget(tmp_path/'budget')
    before=account.settings()
    with pytest.raises(CrawlError):configure(account,site_usd=value)
    assert account.settings()==before


def test_saved_site_and_monthly_limits_survive_an_old_browser_save(tmp_path):
    account=DailyBudget(tmp_path/'budget')
    configure(account,site_usd=5,daily_usd=20,monthly_usd=500)
    account.configure(dict(enabled=False,daily_usd=20,min_grade=2,grading_criteria='',revision=1))
    assert account.settings()['site_usd']==5 and account.settings()['monthly_usd']==500
    assert not account.settings()['enabled']


def test_legacy_cap_increase_reuses_paid_cache_and_counts_prior_actual_and_uncertain_cost(candidate,tmp_path):
    _,row=candidate;capture={**row['captures'][0],'archive_path':'websites/guild.example/20000101000000/index.html'}
    account=DailyBudget(tmp_path/'budget');configure(account,site_usd=5,daily_usd=20,monthly_usd=500)
    calls=[]
    class Luna:
        def __init__(self,_):pass
        def request(self,payload):calls.append(payload);return response()
        def close(self):pass
    directory=tmp_path/'enrichment'
    with account.bind(purpose='enrichment'):
        client=Enricher(directory,'dummy',maximum=2,client_factory=Luna)
        client.store.db.execute("INSERT INTO attempts(reserved,actual,status) VALUES (3,1.95,'completed')")
        client.store.db.execute("INSERT INTO attempts(reserved,status) VALUES (.04,'reserved')")
        client.store.db.commit()
        assert client.enrich(capture,SOURCE)
        client.close()
        client=Enricher(directory,'dummy',maximum=2,client_factory=Luna)
        try:
            assert client.enrich(capture,SOURCE) and len(calls)==1
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==3
            # Earlier cost is retained, including an unresolved reservation.
            client.store.db.execute("INSERT INTO attempts(reserved,status) VALUES (3.01,'reserved')")
            client.store.db.commit()
            before=account.snapshot()['unresolved_usd']
            assert client.enrich(capture,SOURCE)  # A funded or exhausted cap never charges cache hits.
            with pytest.raises(CrawlError,match='dollar budget'):client.enrich(capture,SOURCE+' Next source.')
            assert len(calls)==1 and account.snapshot()['unresolved_usd']==before
            assert client.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==4
        finally:client.close()


def test_site_limit_is_atomic_across_workers_and_never_renews_after_calendar_resets(tmp_path,monkeypatch):
    when=datetime(2026,11,30,23,59,tzinfo=timezone.utc);monkeypatch.setattr('daily_budget.instant',lambda:when)
    account=DailyBudget(tmp_path/'budget');configure(account,site_usd=5,daily_usd=20,monthly_usd=500)
    directory=tmp_path/'site'
    with closing(Store(directory)) as store:
        store.db.execute("INSERT INTO attempts(reserved,actual,status) VALUES (4.6,4.6,'completed')");store.db.commit()
    receipt=account.reserve(4.6);account.settle(receipt,4.6)
    def reserve(_):
        with account.bind(purpose='enrichment'),closing(Store(directory)) as store:
            store.db.execute('BEGIN IMMEDIATE')
            attempt=store.db.execute("INSERT INTO attempts(reserved,status) VALUES (.2,'reserved')").lastrowid
            try:reserve_attempt(store,attempt,.2,site_maximum=2)
            except SiteBudgetExceeded:return False
            store.db.commit();return True
    with ThreadPoolExecutor(max_workers=6) as pool:assert sum(pool.map(reserve,range(6)))==2
    assert account.snapshot()['estimated_usd']==4.6 and account.snapshot()['unresolved_usd']==.4
    when+=timedelta(minutes=2)
    assert account.snapshot()['monthly']['estimated_usd']==0
    assert reserve(0) is False
    with closing(Store(directory)) as store:
        assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==3


def test_reservation_uses_current_site_setting_under_the_shared_admission_lock(tmp_path):
    account=DailyBudget(tmp_path/'budget');configure(account,site_usd=5,daily_usd=20,monthly_usd=500)
    assert account.reserve(.1,site_spent_units=units(4.8),site_maximum=2)
    configure(account,site_usd=4)
    with pytest.raises(SiteBudgetExceeded):account.reserve(.1,site_spent_units=units(4),site_maximum=5)


def test_unconfigured_account_and_standalone_import_keep_original_finite_limits(tmp_path):
    account=DailyBudget(tmp_path/'budget')
    with pytest.raises(SiteBudgetExceeded):account.reserve(.1,site_spent_units=units(2),site_maximum=2)
    with closing(Store(tmp_path/'standalone')) as store:
        store.db.execute("INSERT INTO attempts(reserved,status) VALUES (5,'reserved')");store.db.commit()
        store.db.execute('BEGIN IMMEDIATE')
        attempt=store.db.execute("INSERT INTO attempts(reserved,status) VALUES (.1,'reserved')").lastrowid
        with pytest.raises(SiteBudgetExceeded):reserve_attempt(store,attempt,.1,site_maximum=5)
        assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==1



def test_site_setting_requires_compatible_foreground_workers_but_never_blocks_on_reindex(tmp_path):
    import automation
    root=tmp_path/'portal'
    with closing(Store(root)):pass
    account=automation.budget(root)
    job={'metadata':{'name':'old-import','labels':{'app':'eqarchives-capture-import'}},'spec':{
        'template':{'spec':{'containers':[{'env':[{'name':'LUNA_DAILY_BUDGET_ROOT','value':'/budget'},
        {'name':'LUNA_MONTHLY_BUDGET_VERSION','value':'calendar-v1'}]}]}}},'status':{'active':1}}
    class Kube:
        def jobs(self):return [job,{'metadata':{'name':'full-reindex','labels':{}},'spec':{},'status':{'active':1}}]
    payload=dict(enabled=False,daily_usd=20,monthly_usd=500,site_usd=5,min_grade=2,grading_criteria='',revision=0)
    with pytest.raises(CrawlError,match='per-site budget control'):automation.configure(root,payload,Kube())
    assert account.settings()['configured'] is False
    job['spec']['template']['spec']['containers'][0]['env'].append({'name':'LUNA_SITE_BUDGET_VERSION','value':'site-v1'})
    assert automation.configure(root,payload,Kube())['settings']['site_usd']==5


@pytest.mark.parametrize('kwargs',[{'site_spent_units':True,'site_maximum':5},
    {'site_spent_units':-1,'site_maximum':5},{'site_spent_units':0},
    {'site_maximum':5},{'site_spent_units':0,'site_maximum':0}])
def test_invalid_site_admission_never_creates_a_reservation(tmp_path,kwargs):
    account=DailyBudget(tmp_path/'budget')
    with pytest.raises(CrawlError):account.reserve(.1,**kwargs)
    assert account.snapshot()['unresolved_usd']==0
