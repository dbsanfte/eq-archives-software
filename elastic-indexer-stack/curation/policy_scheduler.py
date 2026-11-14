"""Admit one disposable policy slice, yielding to imports and legacy writers."""
import json
import os
from pathlib import Path
import re
import time
import math
import threading

from common import CrawlError,save
from jobs import finished, import_job
from policy_maintenance import APP, identity
from indexer import gemma, passages
from state import connect

CONTROL_LOCK=threading.Lock()


def write_control(root,enabled):
    """Atomic, infrequently rewritten hints; workers never open portal SQLite."""
    with CONTROL_LOCK:
        value={'key':identity(),'enabled':enabled,'foreground':import_pending(root),'updated_at':time.time()}
        path=Path(root)/'indexing-state/policy-control.json'
        try:
            old=json.loads(path.read_text())
            if all(old.get(k)==value[k] for k in ('key','enabled','foreground')) and old.get('updated_at',0)>time.time()-60:return
        except (OSError,ValueError,TypeError):pass
        path.parent.mkdir(mode=0o770,parents=True,exist_ok=True)
        save(path,value);path.chmod(0o660)

def import_pending(root,*,budget_wait=False):
    with connect(root) as store:
        states=('published_waiting_index','indexing')+(('index_budget_waiting',) if budget_wait else ())
        return store.db.execute(f"SELECT 1 FROM batches WHERE state IN ({','.join('?' for _ in states)}) LIMIT 1",states).fetchone() is not None


def job_for(name,image):
    if not re.fullmatch(r'eqarchives-policy-[a-f0-9]{16}-[1-9][0-9]{0,8}',name):
        raise CrawlError('Invalid policy maintenance identity')
    job=import_job({'id':'0'*32,'manifest_sha256':'0'*64},image)
    job['metadata']={'name':name,'namespace':'eqarchives-es','labels':{'app':APP},
                     'annotations':{'eqarchives.org/policy':passages.VERSION,'eqarchives.org/model':gemma.MODEL}}
    job['spec'].update(backoffLimit=0,activeDeadlineSeconds=900,ttlSecondsAfterFinished=86400)
    pod=job['spec']['template']['spec'];job['spec']['template']['metadata']['labels']={'app':APP}
    pod['nodeSelector']={'kubernetes.io/hostname':'eqvm'}
    container=pod['containers'][0]
    container.update(name='policy',command=['python','/app/curation/policy_maintenance.py','--root','/data'],
        resources={'requests':{'cpu':'50m','memory':'256Mi'},'limits':{'cpu':'1','memory':'2Gi'}})
    container['env']=[e for e in container['env'] if e['name'] in ('ELASTICSEARCH_URL','SSL_CERT_FILE','EMBEDDING_URL','EMBEDDING_MODEL','EMBEDDING_CACHE_PATH','EMBEDDING_BATCH_SIZE','EMBEDDING_BATCH_CONCURRENCY')]+[
        {'name':'ELASTICSEARCH_INDEX','valueFrom':{'secretKeyRef':{'name':'eqarchives-policy-indexer-secrets','key':'es_index'}}},
        {'name':'POLICY_INDEX','valueFrom':{'secretKeyRef':{'name':'eqarchives-policy-indexer-secrets','key':'es_index'}}},
        {'name':'POLICY_JOB_NAME','value':name},{'name':'IMPORT_IMAGE','value':image}]
    container['volumeMounts']=[m for m in container['volumeMounts'] if m['mountPath'] in
                             ('/data','/run/secrets','/indexing-state','/usr/local/lib/python3.13/site-packages/certifi/cacert.pem')]+[
        {'name':'sources','mountPath':'/archive-sources','readOnly':True},
        {'name':'archive','mountPath':'/archive','readOnly':True},
        {'name':'temporary','mountPath':'/tmp'}]
    credentials=next(v for v in pod['volumes'] if v['name']=='credentials')
    credentials['projected']['sources']=[
        {'secret':{'name':'eqarchives-policy-indexer-secrets','items':[{'key':'es_username','path':'es_username'},{'key':'es_password','path':'es_password'}]}},
        {'secret':{'name':'search-eqarchives-secrets','items':[{'key':'openai_api_key','path':'openai_api_key'}]}}]
    pod['volumes']+=[{'name':'sources','hostPath':{'path':'/mnt/samsung/eq-archives-html-reindex','type':'Directory'}},
                     {'name':'archive','hostPath':{'path':'/mnt/samsung/eq-archives','type':'Directory'}},
                     {'name':'temporary','emptyDir':{'sizeLimit':'64Mi'}}]
    return job


def receipt(root,state):
    try:
        path=Path(root)/'indexing-state'/(state['name']+'.json')
        if path.stat().st_size>1024*1024:return None
        result=json.loads(path.read_text())
        if (result.get('job')!=state.get('name') or result.get('policy')!=state.get('policy')
                or result.get('image')!=state.get('image')):return None
        if result.get('model')!=gemma.MODEL or result.get('key')!=state.get('key'):return None
        for field in ('scanned','updated','reused','failed','skipped','unresolved'):
            if type(result.get(field)) is not int or not 0<=result[field]<=10**12:return None
        for field in ('next_at','updated_at'):
            if type(result.get(field)) not in (int,float) or not math.isfinite(result[field]) or not 0<=result[field]<10**12:return None
        if type(result.get('terminal')) is not bool or result.get('phase') not in ('bounded','yielded','sweep_waiting','retry_wait'):return None
        issues=result.get('issues',[])
        if not isinstance(issues,list) or len(issues)>5:return None
        if any(not isinstance(row,dict) or any(not isinstance(row.get(k),str) or len(row[k])>16384 for k in ('id','reason')) for row in issues):return None
        return {**{k:result[k] for k in ('scanned','updated','reused','failed','skipped','unresolved','next_at','phase','terminal','updated_at')},
                'issues':[{k:row[k] for k in ('id','reason')} for row in issues]}
    except (OSError,ValueError,KeyError,TypeError):return None


def schedule(root,kube,jobs,image):
    write_control(root,True)
    with connect(root) as store:state=store.get('policy_maintenance',{})
    def saved(**changes):
        state.update(changes,requested_policy=passages.VERSION)
        state.setdefault('policy',passages.VERSION)
        with connect(root) as store:store.set('policy_maintenance',state)
        return state
    own=next((j for j in jobs if j['metadata']['name']==state.get('name')),None)
    if own:
        if (own['metadata'].get('labels',{}).get('app')!=APP
                or own['metadata'].get('annotations',{}).get('eqarchives.org/policy')!=state.get('policy')
                or own['metadata'].get('annotations',{}).get('eqarchives.org/model')!=gemma.MODEL
                or own['spec']['template']['spec']['containers'][0]['image']!=state.get('image')):
            return saved(phase='attention',error='Policy Job identity changed; background repair is paused.')
        progress=receipt(root,state)
        if not finished(own):
            return saved(phase='running',progress=progress,error=None,submitted=True)
        if state.get('reconciled')!=state['name']:
            complete=any(c.get('type')=='Complete' and c.get('status')=='True' for c in own.get('status',{}).get('conditions',[]))
            valid=complete and progress and progress.get('terminal') is True and progress.get('phase') in ('bounded','yielded','sweep_waiting')
            saved(reconciled=state['name'],progress=progress,submitted=True,
                  next_at=progress['next_at'] if valid else time.time()+300,
                  error=None if valid else 'Background policy worker needs retry; committed checkpoints are retained.')
    elif state.get('name') and state.get('reconciled')!=state['name'] and state.get('submitted'):
        progress=receipt(root,state)
        if progress and progress.get('terminal') is True and progress.get('phase') in ('bounded','yielded','sweep_waiting'):
            saved(reconciled=state['name'],progress=progress,next_at=progress['next_at'])
        else:saved(reconciled=state['name'],next_at=time.time()+300,
                   error='The policy Job disappeared; a numbered retry will use its committed checkpoints.')
    if import_pending(root):return saved(phase='waiting_imports')
    waiting=sorted(j['metadata']['name'] for j in jobs if not finished(j) and not j['spec'].get('suspend',False))
    if waiting:return saved(phase='waiting_jobs',waiting_for=waiting)
    if state.get('key')==identity() and state.get('next_at',0)>time.time():
        return saved(phase='retry_wait' if state.get('error') else 'sweep_waiting')
    if not re.fullmatch(r'dbsanfte/frontend@sha256:[a-f0-9]{64}',image):
        raise CrawlError('Policy maintenance requires the deployed immutable image')
    if state.get('name') and not state.get('submitted') and state.get('key')==identity():
        name=state['name']  # Lost create response: reconcile/retry the same name.
    else:
        with connect(root) as store:
            sequence=store.get('policy_sequence:'+identity(),0)+1
            store.set('policy_sequence:'+identity(),sequence)
        if sequence>=10**9:raise CrawlError('Policy maintenance attempt limit reached')
        name=f'eqarchives-policy-{identity()}-{sequence}'
        saved(name=name,key=identity(),sequence=sequence,policy=passages.VERSION,image=image,
              submitted=False,phase='submitting',error=None,waiting_for=[])
    try:kube.create(job_for(name,state['image']))
    except Exception:
        saved(phase='checking_submission',error='Policy submission outcome is uncertain; retrying this identity.')
        return state
    jobs.append(job_for(name,state['image']))
    return saved(submitted=True,phase='running',error=None)
