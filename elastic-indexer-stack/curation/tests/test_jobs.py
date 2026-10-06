import json
from pathlib import Path

import pytest

from common import CrawlError, digest
from jobs import blockers, import_job
from state import connect, worker_lease
from worker import Worker
from conftest import manifest_for

IMAGE='dbsanfte/frontend@sha256:'+'1'*64


def test_controller_credentials_mounts_start_without_nested_readonly_volumes(tmp_path,monkeypatch):
    import ssl
    import yaml
    from jobs import Kubernetes
    resources=yaml.safe_load_all((Path(__file__).resolve().parents[1]/'k8s/curation.yaml').read_text())
    deployment=next(resource for resource in resources if resource['kind']=='Deployment')
    spec=deployment['spec']['template']['spec']
    assert spec.get('automountServiceAccountToken') is False
    container=spec['containers'][0]
    directory=next(value['value'] for value in container['env'] if value['name']=='KUBERNETES_CREDENTIALS_DIR')
    mounts=container['volumeMounts']
    def canonical(path):
        return Path('/run'+path[len('/var/run'):] if path.startswith('/var/run/') else path)
    for mount in mounts:
        if mount.get('readOnly'):
            for other in mounts:
                assert canonical(mount['mountPath']) not in canonical(other['mountPath']).parents
    token_mount=next(mount for mount in mounts if mount['mountPath']==directory)
    assert token_mount['readOnly']
    volume=next(volume for volume in spec['volumes'] if volume['name']==token_mount['name'])
    projection=volume['projected']['sources']
    assert projection[0]['serviceAccountToken']['path']=='token'
    assert projection[1]['configMap']['items']==[{'key':'ca.crt','path':'ca.crt'}]
    assert projection[2]['downwardAPI']['items'][0]['fieldRef']['fieldPath']=='metadata.namespace'
    (tmp_path/'namespace').write_text('eqarchives-es\n')
    monkeypatch.setenv('KUBERNETES_CREDENTIALS_DIR',str(tmp_path))
    monkeypatch.setenv('KUBERNETES_SERVICE_HOST','fixture')
    monkeypatch.setenv('KUBERNETES_SERVICE_PORT','443')
    observed=[]
    monkeypatch.setattr(ssl,'create_default_context',lambda **kwargs:observed.append(kwargs) or object())
    client=Kubernetes()
    assert client.directory==tmp_path and client.namespace=='eqarchives-es'
    assert observed==[{'cafile':str(tmp_path/'ca.crt')}]


def test_kubernetes_paging_checks_later_jobs_and_rereads_rotated_tokens(tmp_path,monkeypatch):
    import io
    import ssl
    from jobs import Kubernetes
    from urllib.parse import parse_qs,urlsplit
    from urllib.error import HTTPError
    client=Kubernetes.__new__(Kubernetes)
    client.directory=tmp_path
    client.namespace='eqarchives-es'
    client.base='https://fixture'
    client.context=ssl.create_default_context()
    (tmp_path/'token').write_text('first-dummy-token')
    seen=[]
    def response(request,context,timeout):
        seen.append(request)
        if len(seen)==1:
            assert request.get_header('Authorization')=='Bearer first-dummy-token'
            (tmp_path/'token').write_text('rotated-dummy-token')
            return io.StringIO(json.dumps({'items':[],'metadata':{'continue':'next /+'}}))
        assert request.get_header('Authorization')=='Bearer rotated-dummy-token'
        assert parse_qs(urlsplit(request.full_url).query)['continue']==['next /+']
        return io.StringIO(json.dumps({'items':[{'metadata':{'name':'legacy-pending'},'spec':{},'status':{}}]}))
    monkeypatch.setattr('jobs.urllib.request.urlopen',response)
    assert blockers(client.jobs(),'new-import')==['legacy-pending']
    assert len(seen)==2
    def failure(*args,**kwargs):raise HTTPError('fixture',403,'private response',{},None)
    monkeypatch.setattr('jobs.urllib.request.urlopen',failure)
    with pytest.raises(CrawlError,match='Kubernetes HTTP 403') as caught:
        client.jobs()
    assert 'private response' not in str(caught.value)


def test_simultaneous_paid_actions_create_only_one_operation(candidate):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from state import enqueue
    root,_=candidate
    ready=threading.Barrier(4)
    def action(_):
        with connect(root) as store:
            ready.wait(timeout=10)
            try:
                enqueue(store,'discover',{'max_candidates':50,'max_usd':2})
                return 'queued'
            except CrawlError:
                return 'blocked'
    with ThreadPoolExecutor(max_workers=4) as executor:
        results=list(executor.map(action,range(4)))
    assert results.count('queued')==1
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==1


def test_new_imports_wait_for_existing_active_pending_and_retrying_jobs(candidate,monkeypatch):
    root,row=candidate
    manifest=manifest_for(row)
    expected=digest(manifest)
    existing={'metadata':{'name':'reindex-text-html-resume-20261006','uid':'existing'},'spec':{},'status':{'active':1}}
    class FakeKube:
        def __init__(self): self.items=[existing]; self.created=[]
        def jobs(self): return self.items.copy()
        def create(self,job): self.created.append(job); self.items.append(job)
    kube=FakeKube()
    monkeypatch.setenv('IMPORT_IMAGE',IMAGE)
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'published_waiting_index',?,?,?,NULL,NULL,'now','now')",
                         (manifest['batch_id'],json.dumps(manifest),expected,json.dumps({'commit':'a'*40})))
        store.db.commit()
    worker=Worker(root,kube=kube)
    try:
        for active in (1,0):
            existing['status']['active']=active
            worker.indexing()
            assert kube.created == []
            with connect(root) as store:
                batch=store.db.execute('SELECT state,job FROM batches').fetchone()
                assert batch[0] == 'published_waiting_index'
                assert json.loads(batch[1])['waiting_for'] == [existing['metadata']['name']]
        existing['status']['conditions']=[{'type':'Complete','status':'True'}]
        worker.indexing()
        worker.indexing()
        assert len(kube.created) == 1
        job=kube.created[0]
        assert job['spec']['template']['spec']['containers'][0]['image'] == IMAGE
        assert job['metadata']['annotations']['eqarchives.org/manifest-sha256'] == expected
        assert existing['metadata']['uid'] == 'existing'
        job['status']={'conditions':[{'type':'Complete','status':'True'}]}
        worker.indexing()
        with connect(root) as store:
            assert store.db.execute('SELECT state FROM batches').fetchone()[0] == 'indexed'
    finally:
        worker.lease.close()


def test_import_job_only_mounts_staging_readonly_and_separate_indexing_secrets():
    batch={'id':'a'*32,'manifest_sha256':'b'*64}
    spec=import_job(batch,IMAGE)['spec']['template']['spec']
    assert not spec['automountServiceAccountToken']
    mounts=spec['containers'][0]['volumeMounts']
    assert mounts[0]['readOnly'] and mounts[1]['readOnly']
    assert mounts[2]['subPath']=='enrichment' and mounts[2]['mountPath']=='/enrichment'
    assert not any('hostPath' in volume for volume in spec['volumes'])
    secret_names=[source['secret']['name'] for source in spec['volumes'][1]['projected']['sources']]
    assert secret_names == ['eqarchives-capture-indexer-secrets','search-eqarchives-secrets','eqarchives-curation-secrets']
    assert spec['volumes'][1]['projected']['sources'][2]['secret']['items']==[{'key':'luna_api_key','path':'luna_api_key'}]
    with pytest.raises(CrawlError): import_job(batch,'latest')


def test_exclusive_worker_lease_and_explicit_restart_resume(candidate):
    root,row=candidate
    with connect(root) as store:
        store.db.execute("INSERT INTO operations VALUES (?,'discover','running','{}',NULL,NULL,'now','now')",('b'*32,))
        store.db.commit()
    worker=Worker(root)
    try:
        with pytest.raises(CrawlError,match='Another worker'):
            worker_lease(root)
        with connect(root) as store:
            assert store.db.execute('SELECT state FROM operations').fetchone()[0] == 'interrupted'
    finally:
        worker.lease.close()
