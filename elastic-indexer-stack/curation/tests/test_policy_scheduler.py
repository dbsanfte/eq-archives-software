from copy import deepcopy
import json
import time

import pytest

from common import CrawlError,save
from indexer import gemma,passages
from jobs import blockers
from policy_maintenance import APP,identity
from policy_scheduler import import_pending,job_for,receipt,schedule
from state import connect

IMAGE='dbsanfte/frontend@sha256:'+'1'*64


class Kube:
    def __init__(self,items=()):self.items=list(items);self.created=[]
    def create(self,job):
        self.created.append(deepcopy(job));self.items.append(deepcopy(job))
        return deepcopy(job)


def background(root,kube):return schedule(root,kube,kube.items.copy(),IMAGE)


def complete(root,kube,*,phase='bounded',next_at=0,valid=True):
    with connect(root) as store:state=store.get('policy_maintenance')
    job=next(j for j in kube.items if j['metadata']['name']==state['name'])
    job['status']={'conditions':[{'type':'Complete','status':'True'}]}
    record=dict(job=state['name'],policy=state['policy'],model=gemma.MODEL,image=IMAGE,key=identity(),
                scanned=20,updated=2,reused=17,failed=1,skipped=0,unresolved=1,next_at=next_at,
                phase=phase,terminal=True,updated_at=time.time())
    if not valid:record['image']='wrong-image'
    (root/'indexing-state').mkdir(exist_ok=True)
    save(root/'indexing-state'/(state['name']+'.json'),record)
    return state


def test_import_slot_ignores_every_background_and_legacy_job_but_serializes_pending_imports():
    legacy={'metadata':{'name':'reindex-running'},'spec':{},'status':{'active':1}}
    policy=job_for('eqarchives-policy-'+identity()+'-1',IMAGE)
    peer={'metadata':{'name':'other-import','labels':{'app':'eqarchives-capture-import'}},'spec':{},'status':{'active':0}}
    original=deepcopy([legacy,policy,peer])
    assert blockers([legacy,policy],'own')==[]
    assert blockers([legacy,policy,peer],'own')==['other-import']
    assert [legacy,policy,peer]==original
    peer['spec']['suspend']=True;assert blockers([peer],'own')==[]


def test_policy_job_has_no_paid_publication_cluster_credentials_or_archive_write_mounts():
    job=job_for('eqarchives-policy-'+identity()+'-1',IMAGE);pod=job['spec']['template']['spec'];container=pod['containers'][0]
    assert job['spec']['activeDeadlineSeconds']==900 and job['spec']['ttlSecondsAfterFinished']==86400
    assert job['spec']['backoffLimit']==0 and pod['automountServiceAccountToken'] is False
    assert pod['securityContext']['runAsUser']==10001 and container['securityContext']['readOnlyRootFilesystem']
    secrets=[s['secret']['name'] for v in pod['volumes'] if v['name']=='credentials' for s in v['projected']['sources']]
    assert secrets==['eqarchives-policy-indexer-secrets','search-eqarchives-secrets']
    writable=[m for m in container['volumeMounts'] if not m.get('readOnly')]
    assert {m['mountPath'] for m in writable}=={'/indexing-state','/tmp'}
    assert not any(e['name'].startswith('LUNA_') for e in container['env'])
    assert next(e['value'] for e in container['env'] if e['name']=='EMBEDDING_URL').startswith('http://nomic-embeddings-rtx3080.')
    assert next(e for e in container['env'] if e['name']=='ELASTICSEARCH_INDEX')['valueFrom']['secretKeyRef']['key']=='es_index'
    for name,image in [('invalid',IMAGE),('eqarchives-policy-'+identity()+'-1','latest')]:
        with pytest.raises(CrawlError):job_for(name,image)


def test_legacy_jobs_delay_only_policy_sweeps_and_existing_jobs_are_never_modified(tmp_path):
    legacy={'metadata':{'name':'existing-full-reindex','uid':'original'},'spec':{},'status':{'active':0}}
    original=deepcopy(legacy);kube=Kube([legacy])
    result=background(tmp_path,kube)
    assert result['phase']=='waiting_jobs' and not kube.created and legacy==original
    legacy['status']={'conditions':[{'type':'Complete','status':'True'}]}
    result=background(tmp_path,kube)
    assert result['phase']=='running' and len(kube.created)==1
    assert background(tmp_path,kube)['phase']=='running' and len(kube.created)==1


def test_ready_imports_have_priority_but_unfunded_or_failed_imports_do_not_block_free_repairs(tmp_path):
    with connect(tmp_path) as store:
        store.db.execute("INSERT INTO batches VALUES ('site','published_waiting_index','{}','',NULL,NULL,NULL,'now','now')");store.db.commit()
    kube=Kube();assert background(tmp_path,kube)['phase']=='waiting_imports' and not kube.created
    with connect(tmp_path) as store:
        store.db.execute("UPDATE batches SET state='index_budget_waiting'");store.db.commit()
    assert import_pending(tmp_path,budget_wait=True) and not import_pending(tmp_path)
    assert background(tmp_path,kube)['phase']=='running'


def test_bounded_slices_continue_from_receipts_and_completed_sweeps_wait(tmp_path):
    kube=Kube();first=background(tmp_path,kube);name=first['name']
    complete(tmp_path,kube)
    second=background(tmp_path,kube)
    assert second['name']!=name and second['sequence']==2 and len(kube.created)==2
    assert second['progress']['reused']==17 and second['progress']['unresolved']==1
    complete(tmp_path,kube,phase='sweep_waiting',next_at=time.time()+3600)
    assert background(tmp_path,kube)['phase']=='sweep_waiting' and len(kube.created)==2


def test_invalid_completion_and_worker_failures_retry_without_claiming_completion(tmp_path):
    kube=Kube();background(tmp_path,kube);complete(tmp_path,kube,valid=False)
    state=background(tmp_path,kube)
    assert state['phase']=='retry_wait' and state['error'] and len(kube.created)==1
    with connect(tmp_path) as store:
        value=store.get('policy_maintenance');value['next_at']=0;store.set('policy_maintenance',value)
    second=background(tmp_path,kube);assert second['sequence']==2
    kube.items[-1]['status']={'conditions':[{'type':'Failed','status':'True'}]}
    assert background(tmp_path,kube)['phase']=='retry_wait'


def test_uncertain_create_reuses_identity_until_reconciled_and_cannot_duplicate_work(tmp_path):
    class Lost(Kube):
        def create(self,job):
            super().create(job)
            raise OSError('Lost response with private details')
    kube=Lost();state=background(tmp_path,kube)
    assert state['phase']=='checking_submission' and 'private' not in state['error']
    assert background(tmp_path,kube)['name']==state['name'] and len(kube.created)==1
    # An absent response/Job is retried with its exact original name. Once a
    # Job has been observed, disappearance needs verification instead.
    kube.items.clear();assert background(tmp_path,kube)['phase']=='retry_wait'
    class Missing(Kube):
        def create(self,job):
            self.created.append(deepcopy(job));raise OSError('request never arrived')
    missing=Missing();other=tmp_path/'other'
    background(other,missing);background(other,missing)
    assert len(missing.created)==2 and missing.created[0]['metadata']['name']==missing.created[1]['metadata']['name']


def test_changed_job_identity_and_missing_unverified_job_pause_only_background(tmp_path):
    kube=Kube();state=background(tmp_path,kube)
    kube.items[0]['spec']['template']['spec']['containers'][0]['image']='unexpected'
    assert background(tmp_path,kube)['phase']=='attention'
    kube.items.clear()
    assert background(tmp_path,kube)['phase']=='retry_wait'
    assert blockers(kube.items,'new-import')==[]


def test_ttl_retains_verified_receipts_and_policy_changes_start_a_fresh_cursor(tmp_path,monkeypatch):
    kube=Kube();background(tmp_path,kube);state=complete(tmp_path,kube,next_at=time.time()+3600)
    kube.items.clear()
    assert background(tmp_path,kube)['phase']=='sweep_waiting'
    assert receipt(tmp_path,state)['terminal'] is True
    monkeypatch.setattr(passages,'VERSION','eq-passages-480-v999')
    result=background(tmp_path,kube)
    assert result['policy']==passages.VERSION and result['sequence']==1 and result['key']!=state['key']
