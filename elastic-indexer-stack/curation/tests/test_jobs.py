import json
from pathlib import Path

import pytest

from import_status import write_status
from common import CrawlError, digest
from jobs import blockers, import_job, import_name
from state import connect, worker_lease
from worker import Worker
from conftest import manifest_for

IMAGE='dbsanfte/frontend@sha256:'+'1'*64


def test_imports_share_only_the_private_embedding_cache_subdirectory():
    batch={'id':'a'*32,'manifest_sha256':'b'*64}
    job=import_job(batch,IMAGE);pod=job['spec']['template']['spec'];container=pod['containers'][0]
    env={row['name']:row.get('value') for row in container['env']}
    assert env['EMBEDDING_CACHE_PATH']=='/indexing-state/embeddings.sqlite'
    assert env['EMBEDDING_BATCH_SIZE']=='8' and env['EMBEDDING_BATCH_CONCURRENCY']=='4'
    assert {'name':'staging','mountPath':'/indexing-state','subPath':'indexing-state'} in container['volumeMounts']
    assert container['volumeMounts'][0]['readOnly'] and pod['securityContext']['runAsUser']==10001


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
        return io.StringIO(json.dumps({'items':[{'metadata':{'name':'legacy-pending','labels':{'app':'eqarchives-capture-import'}},'spec':{},'status':{}}]}))
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


def test_new_imports_wait_for_other_active_pending_and_retrying_foreground_imports(candidate,monkeypatch):
    root,row=candidate
    manifest=manifest_for(row)
    expected=digest(manifest)
    existing={'metadata':{'name':'other-capture-import','uid':'existing','labels':{'app':'eqarchives-capture-import'}},'spec':{},'status':{'active':1}}
    class FakeKube:
        def __init__(self): self.items=[existing]; self.created=[]
        def jobs(self): return self.items.copy()
        def create(self,job): self.created.append(job); self.items.append(job)
    kube=FakeKube()
    monkeypatch.setenv('IMPORT_IMAGE',IMAGE)
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'published_waiting_index',?,?,?,NULL,NULL,'now','now')",
                         (manifest['batch_id'],json.dumps(manifest),expected,json.dumps({'commit':'a'*40})))
        store.db.execute("UPDATE candidates SET state='published'")
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
        (root/'enrichment'/manifest['batch_id']).mkdir(parents=True, exist_ok=True)
        write_status(root/'enrichment'/manifest['batch_id'], job['metadata']['name'], expected, 'completed')
        worker.indexing()
        with connect(root) as store:
            assert store.db.execute('SELECT state FROM batches').fetchone()[0] == 'indexed'
    finally:
        worker.lease.close()


def test_import_job_only_mounts_staging_readonly_and_separate_indexing_secrets():
    batch={'id':'a'*32,'manifest_sha256':'b'*64}
    spec=import_job(batch,IMAGE)['spec']['template']['spec']
    assert not spec['automountServiceAccountToken']
    assert next(row['value'] for row in spec['containers'][0]['env'] if row['name']=='EMBEDDING_URL')=='http://cuda-batch-inference.eqarchives-es.svc.cluster.local:8080'
    mounts=spec['containers'][0]['volumeMounts']
    assert mounts[0]['readOnly'] and mounts[1]['readOnly']
    assert mounts[2]['subPath']=='enrichment' and mounts[2]['mountPath']=='/enrichment'
    assert mounts[3]['subPath']=='luna-budget' and mounts[3]['mountPath']=='/luna-budget'
    assert {'name':'LUNA_DAILY_BUDGET_ROOT','value':'/luna-budget'} in spec['containers'][0]['env']
    assert not any('hostPath' in volume for volume in spec['volumes'])
    secret_names=[source['secret']['name'] for source in spec['volumes'][1]['projected']['sources']]
    assert secret_names == ['eqarchives-capture-indexer-secrets','search-eqarchives-secrets','eqarchives-curation-secrets']
    assert spec['volumes'][1]['projected']['sources'][2]['secret']['items']==[{'key':'luna_api_key','path':'luna_api_key'}]
    assert next(row['value'] for row in spec['containers'][0]['env'] if row['name']=='IMPORT_JOB_NAME')==import_name(batch)
    with pytest.raises(CrawlError): import_job(batch,'latest')


def test_failed_job_exposes_only_its_manifest_bound_import_diagnostic(candidate):
    from import_status import EnrichmentError, write_status
    from server import create_app
    from test_server import call
    root,row=candidate;batch=failed_import(root,row)
    job=import_job(batch,IMAGE)
    job['status']={'conditions':[{'type':'Failed','status':'True'}]}
    class FakeKube:
        def jobs(self):return [job]
        def create(self,*args):raise AssertionError('Do not restart the failed Job')
    directory=root/'enrichment'/batch['id'];directory.mkdir(parents=True)
    write_status(directory,job['metadata']['name'],batch['manifest_sha256'],'failed',EnrichmentError('correction_limit','source_evidence'))
    with connect(root) as store:
        store.db.execute("UPDATE batches SET state='indexing'");store.db.commit()
    worker=Worker(root,kube=FakeKube())
    try:
        worker.indexing()
        detail=call(create_app(root,start_worker=False),'GET','/api/candidate?id='+row['id']).json()
        assert detail['review']['state']=='index_failed'
        assert 'two correction attempts' in detail['review']['error']
        assert 'verbatim source excerpt' in detail['review']['error']
        assert detail['review']['job']['name']==job['metadata']['name']
    finally:worker.lease.close()


def failed_import(root,row):
    manifest=manifest_for(row)
    batch={'id':manifest['batch_id'],'state':'index_failed','manifest':manifest,
           'manifest_sha256':digest(manifest),'publication':{'commit':'b'*40}}
    batch['job']={'name':import_name(batch),'state':'index_failed'}
    path=root/'batches'/batch['id']/'approved.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(batch))
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'index_failed',?,?,?,?,NULL,'now','now')",
                         (batch['id'],json.dumps(manifest),batch['manifest_sha256'],json.dumps(batch['publication']),json.dumps(batch['job'])))
        store.db.execute("UPDATE candidates SET state='published',coverage=?",
                         (json.dumps({'capture':{'batch_id':batch['id'],'review_id':batch['id']}}),))
        store.db.commit()
    return batch


def test_slow_index_retry_verification_keeps_http_health_responsive(candidate, monkeypatch):
    import asyncio
    import threading
    import httpx
    import index_captures
    from server import create_app
    from test_server import ORIGIN
    root, row = candidate
    batch = failed_import(root, row)
    app = create_app(root, start_worker=False)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = index_captures.read_batch
    def slow(*args):
        entered.set()
        # An external safety timer also releases the old blocking implementation,
        # so a regression fails rather than deadlocking this test's event loop.
        assert release.wait(5)
        try:return original(*args)
        finally:finished.set()
    monkeypatch.setattr(index_captures, 'read_batch', slow)
    timer = threading.Timer(2, release.set)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('192.168.50.20',1234)),base_url=ORIGIN) as client:
            pending = asyncio.create_task(client.post('/api/index-retry',json={
                'id':batch['id'],'manifest_sha256':batch['manifest_sha256'],'job_name':batch['job']['name']},
                headers={'Origin':ORIGIN,'X-Curation-Request':'1'}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                response = await asyncio.wait_for(client.get('/healthz'), 1)
                assert response.status_code == 200 and not finished.is_set()
            finally:
                release.set()
                assert (await pending).status_code == 202
    try:
        timer.start();asyncio.run(run())
    finally:
        release.set();timer.cancel();timer.join()


def test_retry_verifies_sources_without_holding_the_database_writer(candidate, monkeypatch):
    import index_captures
    from server import create_app
    from test_server import call
    root, row = candidate
    batch = failed_import(root, row)
    original = index_captures.read_batch
    def verify(*args):
        with connect(root) as writer:
            writer.db.execute('PRAGMA busy_timeout=0')
            writer.db.execute('BEGIN IMMEDIATE')
            writer.db.execute("INSERT INTO meta VALUES ('concurrent-import','true')")
            writer.db.commit()
        return original(*args)
    monkeypatch.setattr(index_captures, 'read_batch', verify)
    response = call(create_app(root,start_worker=False),'POST','/api/index-retry',{
        'id':batch['id'],'manifest_sha256':batch['manifest_sha256'],'job_name':batch['job']['name']})
    assert response.status_code == 202
    with connect(root) as store:assert store.get('concurrent-import') is True


def test_busy_index_retry_returns_retryable_503_without_changing_the_approval(candidate):
    from server import create_app
    from test_server import call
    root, row = candidate
    batch = failed_import(root, row)
    app = create_app(root, start_worker=False)
    payload = {'id':batch['id'],'manifest_sha256':batch['manifest_sha256'],'job_name':batch['job']['name']}
    with connect(root) as holder:
        holder.db.execute('BEGIN IMMEDIATE')
        response = call(app,'POST','/api/index-retry',payload)
        assert response.status_code == 503 and response.headers['retry-after'] == '1'
        assert 'retained' in response.json()['error']
        assert holder.db.execute('SELECT state FROM batches').fetchone()[0] == 'index_failed'
        assert not holder.db.execute("SELECT 1 FROM events WHERE action='site_indexing_retried'").fetchone()
        holder.db.rollback()
    assert call(app,'POST','/api/index-retry',payload).status_code == 202


@pytest.mark.parametrize('changed', ['state', 'job', 'publication', 'manifest_sha256'])
def test_retry_rechecks_current_approval_after_source_io(candidate, monkeypatch, changed):
    import index_captures
    from server import create_app
    from test_server import call
    root, row = candidate
    batch = failed_import(root, row)
    original = index_captures.read_batch
    values = {'state':'indexing_declined', 'job':json.dumps({'name':'replaced-job'}),
              'publication':json.dumps({'commit':'c'*40}), 'manifest_sha256':'d'*64}
    def replace(*args):
        saved = original(*args)
        with connect(root) as writer:
            writer.db.execute(f'UPDATE batches SET {changed}=? WHERE id=?', (values[changed],batch['id']))
            writer.db.commit()
        return saved
    monkeypatch.setattr(index_captures, 'read_batch', replace)
    response = call(create_app(root,start_worker=False),'POST','/api/index-retry',{
        'id':batch['id'],'manifest_sha256':batch['manifest_sha256'],'job_name':batch['job']['name']})
    assert response.status_code == 409
    with connect(root) as store:
        assert store.db.execute(f'SELECT {changed} FROM batches').fetchone()[0] == values[changed]
        assert not store.db.execute("SELECT 1 FROM events WHERE action='site_indexing_retried'").fetchone()


def test_explicit_index_retry_preserves_failed_jobs_publication_and_approved_site(candidate,monkeypatch):
    import copy
    from server import create_app
    from test_server import call
    root,row=candidate
    batch=failed_import(root,row)
    app=create_app(root,start_worker=False)
    payload={'id':batch['id'],'manifest_sha256':batch['manifest_sha256'],'job_name':batch['job']['name']}
    assert call(app,'GET','/api/queue?filter=captured').json()['candidates'][0]['review_state']=='index_failed'
    source=root/batch['manifest']['captures'][0]['path']
    stamp=source.stat().st_mtime_ns
    assert call(app,'POST','/api/index-retry',payload).json()=={'state':'published_waiting_index','attempt':2}
    assert call(app,'GET','/api/queue?filter=captured').json()['candidates'][0]['review_state']=='published_waiting_index'
    assert call(app,'POST','/api/index-retry',payload).status_code==409
    assert call(create_app(root,start_worker=False),'GET','/api/site?id='+batch['id']).json()['job']['attempt']==2
    original=import_job(batch,IMAGE)
    original['metadata']['uid']='preserved-failed-job'
    original['status']={'conditions':[{'type':'Failed','status':'True'}]}
    snapshot=copy.deepcopy(original)
    ingestion={'metadata':{'name':'legacy-ingestion','uid':'preserved-ingestion','labels':{'app':'eqarchives-capture-import'}},'spec':{},'status':{'active':0}}
    class FakeKube:
        def __init__(self):self.items=[original,ingestion];self.created=[]
        def jobs(self):return self.items.copy()
        def create(self,job):self.created.append(job);self.items.append(job)
    kube=FakeKube()
    monkeypatch.setenv('IMPORT_IMAGE',IMAGE)
    worker=Worker(root,kube=kube)
    try:
        worker.indexing()
        assert not kube.created
        ingestion['status']['conditions']=[{'type':'Complete','status':'True'}]
        worker.indexing();worker.indexing()
        assert len(kube.created)==1
        retried=kube.created[0]
        assert retried['metadata']['name']==batch['job']['name']+'-r2'
        assert retried['metadata']['annotations']['eqarchives.org/manifest-sha256']==batch['manifest_sha256']
        assert retried['spec']['template']['spec']['containers'][0]['command']==original['spec']['template']['spec']['containers'][0]['command']
        assert original==snapshot
        retried['status']={'conditions':[{'type':'Complete','status':'True'}]}
        (root/'enrichment'/batch['id']).mkdir(parents=True, exist_ok=True)
        write_status(root/'enrichment'/batch['id'], retried['metadata']['name'], batch['manifest_sha256'], 'completed')
        worker.indexing()
        with connect(root) as store:
            saved=store.db.execute('SELECT state,manifest,publication,job FROM batches').fetchone()
            assert saved[0]=='indexed' and json.loads(saved[1])==batch['manifest'] and json.loads(saved[2])==batch['publication']
            assert json.loads(saved[3])['attempt']==2
            assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
            assert store.db.execute('SELECT state FROM candidates').fetchone()[0]=='indexed'
        assert source.stat().st_mtime_ns==stamp
    finally:worker.lease.close()


@pytest.mark.parametrize('problem',['hash','job','source','approval','publication','state'])
def test_index_retry_rejects_stale_or_unverified_approvals(candidate,problem):
    from server import create_app
    from test_server import call
    root,row=candidate
    batch=failed_import(root,row)
    payload={'id':batch['id'],'manifest_sha256':batch['manifest_sha256'],'job_name':batch['job']['name']}
    if problem=='hash':payload['manifest_sha256']='stale'
    elif problem=='job':payload['job_name']='old-job'
    elif problem=='source':(root/batch['manifest']['captures'][0]['path']).write_bytes(b'tampered')
    elif problem=='approval':(root/'batches'/batch['id']/'approved.json').unlink()
    else:
        with connect(root) as store:
            if problem=='publication':store.db.execute('UPDATE batches SET publication=?',(json.dumps({'commit':'c'*40}),))
            else:store.db.execute("UPDATE batches SET state='indexing'")
            store.db.commit()
    assert call(create_app(root,start_worker=False),'POST','/api/index-retry',payload).status_code==409
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
        assert json.loads(store.db.execute('SELECT job FROM batches').fetchone()[0])==batch['job']


def test_simultaneous_index_retries_queue_only_one_attempt(candidate):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from server import create_app
    from test_server import call
    root,row=candidate
    batch=failed_import(root,row)
    app=create_app(root,start_worker=False)
    payload={'id':batch['id'],'manifest_sha256':batch['manifest_sha256'],'job_name':batch['job']['name']}
    ready=threading.Barrier(2)
    def retry(_):
        ready.wait(timeout=10)
        return call(app,'POST','/api/index-retry',payload).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(retry,range(2)))==[202,409]


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


def test_import_uses_operator_database_profile_and_verified_combined_trust():
    pod=import_job({'id':'a'*32,'manifest_sha256':'b'*64},IMAGE)['spec']['template']['spec']
    env={e['name']:e for e in pod['containers'][0]['env']}
    assert env['ELASTICSEARCH_URL']['valueFrom']['configMapKeyRef']=={'name':'eqarchives-database-client','key':'url'}
    mount=next(m for m in pod['containers'][0]['volumeMounts'] if m['name']=='database-trust')
    assert mount['readOnly'] and mount['subPath']=='ca-bundle.pem'
    assert mount['mountPath']==env['SSL_CERT_FILE']['value']=='/usr/local/lib/python3.13/site-packages/certifi/cacert.pem'
    assert next(v for v in pod['volumes'] if v['name']=='database-trust')['configMap']['name']=='eqarchives-database-client'


def test_import_profile_can_start_the_pinned_gemma_tokenizer():
    container=import_job({'id':'a'*32,'manifest_sha256':'b'*64},IMAGE)['spec']['template']['spec']['containers'][0]
    assert container['resources']=={'requests':{'cpu':'100m','memory':'512Mi'},'limits':{'cpu':'1','memory':'2Gi'}}


def test_oom_diagnostics_are_bound_to_the_exact_job_owner_and_do_not_read_logs():
    from jobs import Kubernetes
    from urllib.parse import parse_qs,urlsplit
    client=Kubernetes.__new__(Kubernetes);client.namespace='eqarchives-es';seen=[]
    name='eqarchives-captures-'+'a'*32;job={'metadata':{'name':name,'uid':'owned-job'}}
    def pod(owner):return {'metadata':{'ownerReferences':[{'kind':'Job','uid':owner,'controller':True}]},
        'status':{'containerStatuses':[{'name':'index','state':{'terminated':{'reason':'OOMKilled'}}}]}}
    def response(method,path):
        seen.append(path);assert method=='GET' and '/pods?' in path and '/log' not in path
        assert parse_qs(urlsplit(path).query)['labelSelector']==['job-name='+name]
        return {'items':[pod('other-job')]}
    client.request=response
    assert client.job_failure(job) is None
    client.request=lambda *args:{'items':[pod('owned-job')]}
    assert 'OOMKilled' in client.job_failure(job)
    with pytest.raises(CrawlError):client.job_failure({'metadata':{'name':'reindex-legacy','uid':'original'}})
