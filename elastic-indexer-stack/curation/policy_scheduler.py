"""Admit bounded archive maintenance between previously started Jobs."""
import json
import os
from pathlib import Path
import re
import time
from enrichment_worker import MAX_WORKERS as MAX_LUNA_WORKERS
import math

from common import CrawlError,save
from jobs import blockers, finished, import_job
from policy_enrichment import APP as ENRICHMENT_APP, COUNTERS as ENRICHMENT_COUNTERS
from policy_maintenance import APP, identity
from indexer import gemma, passages
from state import connect

ENRICHMENT_STATE='enrichment_maintenance'
ENRICHMENT_PHASES=('bounded','yielded','waiting_configuration','waiting_budget','waiting_imports',
                   'sweep_waiting','retry_wait')


def write_control(root,enabled,*,import_running=False):
    """Atomic, infrequently rewritten hints; workers never open portal SQLite."""
    value={'key':identity(),'enabled':enabled,'foreground':import_running,'updated_at':time.time(),
           'enrichment_enabled':os.environ.get('PASSAGE_REENRICHMENT_ENABLED')=='1'}
    path=Path(root)/'indexing-state/policy-control.json'
    try:
        old=json.loads(path.read_text())
        if all(old.get(k)==value[k] for k in ('key','enabled','foreground','enrichment_enabled')) and old.get('updated_at',0)>time.time()-60:return
    except (OSError,ValueError,TypeError):pass
    path.parent.mkdir(mode=0o770,parents=True,exist_ok=True)
    save(path,value);path.chmod(0o660)


def import_pending(root):
    with connect(root) as store:
        return store.db.execute("""SELECT 1 FROM batches
            WHERE state IN ('published_waiting_index','indexing','index_budget_waiting') LIMIT 1""").fetchone() is not None


def job_for(name,image,*,enrichment=False):
    if not re.fullmatch(r'eqarchives-(?:policy|enrichment)-[a-f0-9]{16}-[1-9][0-9]{0,8}',name):
        raise CrawlError('Invalid policy maintenance identity')
    dedicated=name.startswith('eqarchives-enrichment-')
    app=ENRICHMENT_APP if dedicated else APP
    job=import_job({'id':'0'*32,'manifest_sha256':'0'*64},image)
    job['metadata']={'name':name,'namespace':'eqarchives-es','labels':{'app':app},
                     'annotations':{'eqarchives.org/policy':passages.VERSION,'eqarchives.org/model':gemma.MODEL}}
    job['spec'].update(backoffLimit=0,activeDeadlineSeconds=900,ttlSecondsAfterFinished=86400)
    pod=job['spec']['template']['spec'];job['spec']['template']['metadata']['labels']={'app':app}
    pod['nodeSelector']={'kubernetes.io/hostname':'eqvm'}
    container=pod['containers'][0]
    entrypoint='enrichment_worker.py' if dedicated else 'policy_maintenance.py'
    container.update(name='enrichment' if dedicated else 'policy',command=['python','/app/curation/'+entrypoint,'--root','/data'],
        resources={'requests':{'cpu':'100m','memory':'512Mi'},'limits':{'cpu':'2','memory':'3Gi'}})
    container['env']=[e for e in container['env'] if e['name'] in ('ELASTICSEARCH_URL','SSL_CERT_FILE','EMBEDDING_URL','EMBEDDING_MODEL','EMBEDDING_CACHE_PATH','EMBEDDING_BATCH_SIZE','EMBEDDING_BATCH_CONCURRENCY')]+[
        {'name':'ELASTICSEARCH_INDEX','valueFrom':{'secretKeyRef':{'name':'eqarchives-policy-indexer-secrets','key':'es_index'}}},
        {'name':'POLICY_INDEX','valueFrom':{'secretKeyRef':{'name':'eqarchives-policy-indexer-secrets','key':'es_index'}}},
        {'name':'POLICY_JOB_NAME','value':name},{'name':'IMPORT_IMAGE','value':image}]
    if dedicated:
        container['env'].append({'name':'LUNA_ENRICHMENT_WORKERS','valueFrom':{'configMapKeyRef':{
            'name':'eqarchives-enrichment-tuning','key':'luna_workers','optional':True}}})
    else:
        container['env'].append({'name':'POLICY_PREPARATION_WORKERS','value':'2'})
    container['volumeMounts']=[m for m in container['volumeMounts'] if m['mountPath'] in
                             ('/data','/run/secrets','/indexing-state','/usr/local/lib/python3.13/site-packages/certifi/cacert.pem')]+[
        {'name':'sources','mountPath':'/archive-sources','readOnly':True},
        {'name':'archive','mountPath':'/archive','readOnly':True},
        {'name':'temporary','mountPath':'/tmp'}]
    credentials=next(v for v in pod['volumes'] if v['name']=='credentials')
    credentials['projected']['sources']=[
        {'secret':{'name':'eqarchives-policy-indexer-secrets','items':[{'key':'es_username','path':'es_username'},{'key':'es_password','path':'es_password'}]}},
        {'secret':{'name':'search-eqarchives-secrets','items':[{'key':'openai_api_key','path':'openai_api_key'}]}}]
    if enrichment or dedicated:
        credentials['projected']['sources'].append(
            {'secret':{'name':'eqarchives-curation-secrets','items':[{'key':'luna_api_key','path':'luna_api_key'}]}})
        container['env'].append({'name':'LUNA_DAILY_BUDGET_ROOT','value':'/luna-budget'})
        container['volumeMounts'].append({'name':'staging','mountPath':'/luna-budget','subPath':'luna-budget'})
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
        dedicated=state['name'].startswith('eqarchives-enrichment-')
        phases=ENRICHMENT_PHASES if dedicated else ('bounded','yielded','sweep_waiting','retry_wait')
        if type(result.get('terminal')) is not bool or result.get('phase') not in phases:return None
        issues=result.get('issues',[])
        if not isinstance(issues,list) or len(issues)>5:return None
        if any(not isinstance(row,dict) or any(not isinstance(row.get(k),str) or len(row[k])>16384 for k in ('id','reason')) for row in issues):return None
        execution=result.get('execution',{})
        execution_keys=({'luna_workers','embedding_waves','examined'} if dedicated else
                        {'preparation_workers','documents','stored_vectors_reused','submitted_inputs'})
        if (not isinstance(execution,dict) or set(execution)-execution_keys
                or any(type(v) is not int or not 0<=v<=10**12 for v in execution.values())
                or execution.get('preparation_workers',0)>4 or execution.get('luna_workers',0)>MAX_LUNA_WORKERS):return None
        enrichment=result.get('enrichment')
        if dedicated and enrichment is None:return None
        if enrichment is not None:
            if (not isinstance(enrichment,dict)
                    or set(enrichment)!={*ENRICHMENT_COUNTERS,'phase','next_at','last_reason'}
                    or any(type(enrichment[k]) is not int or not 0<=enrichment[k]<=10**12 for k in ENRICHMENT_COUNTERS)
                    or enrichment['phase'] not in ENRICHMENT_PHASES
                    or type(enrichment['next_at']) not in (int,float) or not 0<=enrichment['next_at']<10**12
                    or (enrichment['last_reason'] is not None and
                        (not isinstance(enrichment['last_reason'],str)
                         or not re.fullmatch('[a-z_]{1,64}',enrichment['last_reason'])))):
                return None
        return {**{k:result[k] for k in ('scanned','updated','reused','failed','skipped','unresolved','next_at','phase','terminal','updated_at')},
                **({'enrichment':enrichment} if enrichment is not None else {}),
                'issues':[{k:row[k] for k in ('id','reason')} for row in issues],'execution':execution}
    except (OSError,ValueError,KeyError,TypeError):return None


def blocks(job, *, enrichment):
    name=job['metadata']['name']
    app=job['metadata'].get('labels',{}).get('app')
    if not enrichment and app==ENRICHMENT_APP and re.fullmatch(r'eqarchives-enrichment-[a-f0-9]{16}-[1-9][0-9]{0,8}',name):
        return False
    if enrichment and app==APP and re.fullmatch(r'eqarchives-policy-[a-f0-9]{16}-[1-9][0-9]{0,8}',name):
        containers=job['spec']['template']['spec']['containers']
        # An old combined slice retains the shared paid cache until it exits.
        return any(e['name']=='LUNA_DAILY_BUDGET_ROOT' for e in containers[0].get('env',[]))
    return True


def schedule(root,kube,jobs,image,*,enrichment=False):
    state_key=ENRICHMENT_STATE if enrichment else 'policy_maintenance'
    lane,app=('enrichment',ENRICHMENT_APP) if enrichment else ('policy',APP)
    with connect(root) as store:state=store.get(state_key,{})
    def saved(**changes):
        state.update(changes,requested_policy=passages.VERSION)
        state.setdefault('policy',passages.VERSION)
        with connect(root) as store:store.set(state_key,state)
        return state
    own=next((j for j in jobs if j['metadata']['name']==state.get('name')),None)
    if own:
        if (own['metadata'].get('labels',{}).get('app')!=app
                or own['metadata'].get('annotations',{}).get('eqarchives.org/policy')!=state.get('policy')
                or own['metadata'].get('annotations',{}).get('eqarchives.org/model')!=gemma.MODEL
                or own['spec']['template']['spec']['containers'][0]['image']!=state.get('image')):
            return saved(phase='attention',error='Policy Job identity changed; background repair is paused.')
        progress=receipt(root,state)
        if not finished(own):
            return saved(phase='running',progress=progress,error=None,submitted=True)
        if state.get('reconciled')!=state['name']:
            complete=any(c.get('type')=='Complete' and c.get('status')=='True' for c in own.get('status',{}).get('conditions',[]))
            valid=complete and progress and progress.get('terminal') is True and (
                enrichment or progress.get('phase') in ('bounded','yielded','sweep_waiting'))
            saved(reconciled=state['name'],progress=progress,submitted=True,
                  next_at=progress['next_at'] if valid else time.time()+300,
                  error=None if valid else 'Background policy worker needs retry; committed checkpoints are retained.')
    elif state.get('name') and state.get('reconciled')!=state['name'] and state.get('submitted'):
        progress=receipt(root,state)
        if progress and progress.get('terminal') is True and (enrichment or progress.get('phase') in ('bounded','yielded','sweep_waiting')):
            saved(reconciled=state['name'],progress=progress,next_at=progress['next_at'])
        else:saved(reconciled=state['name'],next_at=time.time()+300,
                   error='The policy Job disappeared; a numbered retry will use its committed checkpoints.')
    importing=blockers(jobs,'')
    if importing:return saved(phase='waiting_imports',waiting_for=importing)
    waiting=sorted(j['metadata']['name'] for j in jobs if not finished(j) and not j['spec'].get('suspend',False)
                   and blocks(j,enrichment=enrichment))
    if waiting:return saved(phase='waiting_jobs',waiting_for=waiting)
    if state.get('key')==identity() and state.get('next_at',0)>time.time():
        if enrichment and not state.get('error') and state.get('progress'):
            return saved(phase=state['progress']['phase'])
        return saved(phase='retry_wait' if state.get('error') else 'sweep_waiting')
    if not re.fullmatch(r'dbsanfte/frontend@sha256:[a-f0-9]{64}',image):
        raise CrawlError('Policy maintenance requires the deployed immutable image')
    if state.get('name') and not state.get('submitted') and state.get('key')==identity():
        name=state['name']  # Lost create response: reconcile/retry the same name.
    else:
        with connect(root) as store:
            sequence_key=lane+'_sequence:'+identity()
            sequence=store.get(sequence_key,0)+1
            store.set(sequence_key,sequence)
        if sequence>=10**9:raise CrawlError('Policy maintenance attempt limit reached')
        name=f'eqarchives-{lane}-{identity()}-{sequence}'
        saved(name=name,key=identity(),sequence=sequence,policy=passages.VERSION,image=image,
              submitted=False,phase='submitting',error=None,waiting_for=[],enrichment_enabled=enrichment)
    job=job_for(name,state['image'],enrichment=state.get('enrichment_enabled') is True)
    try:kube.create(job)
    except Exception:
        saved(phase='checking_submission',error='Policy submission outcome is uncertain; retrying this identity.')
        return state
    jobs.append(job)
    return saved(submitted=True,phase='running',error=None)
