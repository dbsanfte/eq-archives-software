import json
from contextlib import closing

import pytest

import automation
from common import Store
from daily_budget import EnrichmentPriorityPause
from grading import grade
from crawler import parser
from state import Lane,connect,enqueue,unpack
from test_daily_budget import enabled,settings


def test_discovery_never_queues_while_approved_enrichment_needs_funds(candidate):
    root,_=candidate;account=automation.budget(root)
    account.configure(settings(account,enabled=True,daily_usd=20))
    account.prioritize('portal',True)
    automation.schedule(root,account)
    with connect(root) as store:
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()
        assert store.get('automatic_activity')['phase']=='enrichment_priority'
    account.prioritize('portal',False)
    automation.schedule(root,account)
    with connect(root) as store:assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==1


def test_manual_grading_preserves_evidence_and_has_no_paid_attempt_when_enrichment_has_priority(candidate):
    root,_=candidate;account=automation.budget(root)
    account.configure(settings(account,enabled=True,daily_usd=20));account.prioritize('portal',True)
    args=parser().parse_args(['--work-dir',str(root),'grade','--api-key-file','dummy','--max-candidates','1','--max-usd','2'])
    class NoCalls:
        def request(self,_):raise AssertionError('Discovery cannot spend ahead of approved enrichment')
    with closing(Store(root)) as store,account.bind():
        store.db.execute('UPDATE candidates SET rating=NULL');store.db.commit()
        with pytest.raises(EnrichmentPriorityPause):grade(args,store,client=NoCalls())
        assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
        assert store.db.execute('SELECT rating FROM candidates').fetchone()[0] is None
    assert account.snapshot()['unresolved_usd']==0


@pytest.mark.parametrize('automatic',[False,True])
def test_priority_pauses_resume_original_work_without_resetting_budget_or_deadline(tmp_path,automatic):
    root=tmp_path/'portal';account=automation.budget(root)
    account.configure(settings(account,enabled=automatic,daily_usd=20))
    original={'automatic':automatic,'max_usd':.25,'deadline':123456789,'saved_source':'immutable'}
    with connect(root) as store:
        identifier=enqueue(store,'discover',original)
        store.db.execute("UPDATE operations SET state='interrupted',result=?,error='Enrichment has priority' WHERE id=?",
                         (json.dumps({'automatic_pause':{'reason':'enrichment_priority'}}),identifier));store.db.commit()
    account.prioritize('portal',True)
    automation.resume_owned(root,Lane.CANDIDATES,account)
    with connect(root) as store:assert store.db.execute('SELECT state FROM operations').fetchone()[0]=='interrupted'
    account.prioritize('portal',False);automation.resume_owned(root,Lane.CANDIDATES,account)
    with connect(root) as store:
        operation=unpack(store.db.execute('SELECT * FROM operations').fetchone())
        assert operation['id']==identifier and operation['state']=='queued' and operation['payload']==original
    assert account.settings()['enabled']==automatic


def test_old_daily_only_paid_workers_cannot_bypass_new_monthly_limits(candidate):
    root,_=candidate;account=automation.budget(root)
    job={'metadata':{'name':'old-paid','labels':{'app':'eqarchives-capture-import'}},'spec':{
        'template':{'spec':{'containers':[{'env':[{'name':'LUNA_DAILY_BUDGET_ROOT','value':'/budget'}]}]}}}}
    class Kube:
        def jobs(self):return [job]
    with pytest.raises(Exception,match='monthly budget control'):
        automation.configure(root,{**settings(account,enabled=True,daily_usd=20),'monthly_usd':200},Kube())
    assert account.settings()['configured'] is False
