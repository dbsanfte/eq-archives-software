"""Bounded rolling policy updates, checkpointed between foreground imports.

Body repairs and budgeted enrichment have independent cursors and writes.
Only acknowledged optimistic updates advance repair counts and resolve failures.
"""
import argparse
from collections import deque, Counter
import hashlib
import json
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote, urlencode

import httpx

from common import save
from index_captures import Services
from indexer import gemma, passages, search_enrichment
from indexer.html_extraction import WEBSITE_EXTRACTION_VERSION
from indexer.sqlite_state import SQLiteState
from indexer.embedding_batches import embed_batches, decode_vectors
from indexer.vector_json import dumps as vector_json
from policy_sources import Sources, SourceError, ServiceError
from policy_enrichment import (COLLECTIONS, COUNTERS, FRONTIER_PAGE_SIZE, MAX_HELD_NEWSGROUPS, NEWSGROUP_MESSAGE_SUFFIX,
                               NEWSGROUP_NAME, current_enrichment, run_enrichment)
from policy_preparation import (LEGACY_POLICY, FIELDS as PASSAGE_FIELDS, newer, prepare,
                                verify_prepared,renderer_identity,request_identity)
from policy_pool import PreparationPool, WorkerLost

APP='eqarchives-policy-maintenance'
UPDATE_BATCH_SIZE = 16
UPDATE_BATCH_BYTES = 1024 * 1024
FIELDS=['id','file_type','mime_type','title','text_full','text.text_chunk','text.title','text.context',
        'text.kind','text.source_start','text.source_end','embedding_model','text_chunking_version',
        'text_extraction_version','text_passage_provenance','archive_source_sha256','archive_commit',
        'llm_search_enrichment_version','llm_enrichment_passage_version','llm_enrichment_view_sha256']


def index_identity():
    return os.environ.get('POLICY_INDEX',os.environ.get('ELASTICSEARCH_INDEX','eq-archive'))


def identity():
    return hashlib.sha256(json.dumps([index_identity(),gemma.MODEL,passages.VERSION,WEBSITE_EXTRACTION_VERSION]).encode()).hexdigest()[:16]


class YieldWork(Exception):
    pass


class State:
    def __init__(self,directory):
        self.root=Path(directory);self.db=SQLiteState(self.root/'policy-maintenance.sqlite',durable=True)
        self.root.mkdir(mode=0o770,parents=True,exist_ok=True)
        fd=os.open(self.root/'policy-maintenance.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o660)
        self.lease=os.fdopen(fd,'a')
        try:fcntl.flock(self.lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lease.close();self.db.close()
            raise ServiceError('maintenance_already_running') from None
        (self.root/'policy-maintenance.lock').chmod(0o660)
        with self.db.transaction(attempts=6) as db:
            schema='''CREATE TABLE IF NOT EXISTS policy_cursors (
                key TEXT PRIMARY KEY, cursor TEXT NOT NULL, scanned INTEGER NOT NULL,
                updated INTEGER NOT NULL, reused INTEGER NOT NULL, failed INTEGER NOT NULL,
                skipped INTEGER NOT NULL, next_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS policy_failures (
                key TEXT NOT NULL,id TEXT NOT NULL,source_sha256 TEXT NOT NULL,reason TEXT NOT NULL,
                attempts INTEGER NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,retry_at REAL NOT NULL,
                resolved_at REAL,job TEXT NOT NULL,PRIMARY KEY(key,id,source_sha256));
                CREATE INDEX IF NOT EXISTS policy_failure_retry ON policy_failures(resolved_at,id,key,retry_at);
                CREATE INDEX IF NOT EXISTS policy_failure_recent ON policy_failures(resolved_at,last_seen DESC,id);
                CREATE TABLE IF NOT EXISTS policy_events (
                key TEXT NOT NULL,id TEXT NOT NULL,source_sha256 TEXT,action TEXT NOT NULL,reason TEXT,
                created REAL NOT NULL,job TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS policy_inflight (
                key TEXT PRIMARY KEY,id TEXT NOT NULL,job TEXT NOT NULL,attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS policy_scopes (
                key TEXT PRIMARY KEY,index_name TEXT NOT NULL,model TEXT NOT NULL,policy TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS policy_enrichment (
                key TEXT PRIMARY KEY,cursor TEXT NOT NULL DEFAULT '',updated INTEGER NOT NULL DEFAULT 0,
                reused INTEGER NOT NULL DEFAULT 0,failed INTEGER NOT NULL DEFAULT 0,skipped INTEGER NOT NULL DEFAULT 0,
                phase TEXT NOT NULL DEFAULT 'bounded',next_at REAL NOT NULL DEFAULT 0,last_reason TEXT);'''
            for statement in schema.split(';'):
                if statement.strip():db.execute(statement)
        with self.db.transaction(attempts=6) as db:
            # A same-model policy upgrade first continues the unfinished
            # frontier. The normal sweep later revisits the completed prefix.
            cursor=''
            if not db.execute('SELECT 1 FROM policy_cursors WHERE key=?',(identity(),)).fetchone():
                current=re.fullmatch(r'eq-passages-480-v(\d+)',passages.VERSION)
                for old_cursor,old_policy in db.execute('''SELECT c.cursor,s.policy FROM policy_cursors c
                        JOIN policy_scopes s ON s.key=c.key WHERE s.index_name=? AND s.model=?''',
                        (index_identity(),gemma.MODEL)):
                    old=re.fullmatch(r'eq-passages-480-v(\d+)',old_policy)
                    if current and old and int(old[1])<int(current[1]):cursor=max(cursor,old_cursor)
            db.execute("INSERT OR IGNORE INTO policy_cursors VALUES (?,?,0,0,0,0,0,0)",(identity(),cursor))
            db.execute('INSERT OR IGNORE INTO policy_scopes VALUES (?,?,?,?)',(identity(),index_identity(),gemma.MODEL,passages.VERSION))

    def close(self):
        self.db.close();self.lease.close()

    def inflight(self):
        row=self.db.connection().execute('SELECT id FROM policy_inflight WHERE key=?',(identity(),)).fetchone()
        return row[0] if row else None

    def begin(self,identifier,job):
        with self.db.transaction(attempts=6) as db:
            old=db.execute('SELECT id,job,attempts FROM policy_inflight WHERE key=?',(identity(),)).fetchone()
            attempts=old[2]+int(old[1]!=job) if old and old[0]==identifier else 1
            db.execute('INSERT OR REPLACE INTO policy_inflight VALUES (?,?,?,?)',(identity(),identifier,job,attempts))
        return attempts<=3

    def abandon(self):
        # Cooperative yields and known transport outages are not poison records.
        with self.db.transaction(attempts=6) as db:db.execute('DELETE FROM policy_inflight WHERE key=?',(identity(),))

    def snapshot(self):
        db=self.db.connection();db.row_factory=sqlite3.Row
        result=dict(db.execute('SELECT * FROM policy_cursors WHERE key=?',(identity(),)).fetchone())
        scope='key IN (SELECT key FROM policy_scopes WHERE index_name=? AND model=?)'
        result['unresolved']=db.execute('SELECT COUNT(DISTINCT id) FROM policy_failures WHERE resolved_at IS NULL AND '+scope,(index_identity(),gemma.MODEL)).fetchone()[0]
        result['issues']=[dict(row) for row in db.execute('SELECT id,reason FROM policy_failures WHERE resolved_at IS NULL AND '+scope+' ORDER BY last_seen DESC LIMIT 5',(index_identity(),gemma.MODEL))]
        return result

    def due(self,limit=100):
        return [r[0] for r in self.db.connection().execute('''SELECT id FROM policy_failures WHERE resolved_at IS NULL
            AND key IN (SELECT key FROM policy_scopes WHERE index_name=? AND model=?)
            GROUP BY id HAVING MAX(CASE WHEN key=? THEN retry_at ELSE 0 END)<=? ORDER BY id LIMIT ?''',
            (index_identity(),gemma.MODEL,identity(),time.time(),limit))]

    def deferred(self,identifier):
        return self.db.connection().execute('''SELECT 1 FROM policy_failures
            WHERE key=? AND id=? AND resolved_at IS NULL AND retry_at>? LIMIT 1''',
            (identity(),identifier,time.time())).fetchone() is not None

    def reject(self,identifier,reason,source_hash,job):
        when=time.time();source_hash=source_hash if isinstance(source_hash,str) and re.fullmatch('[0-9a-f]{64}',source_hash) else ''
        retry=when+(300 if reason=='concurrent_update' else 86400)
        with self.db.transaction(attempts=6) as db:
            db.execute('''INSERT INTO policy_failures VALUES (?,?,?,?,1,?,?,?,NULL,?)
                ON CONFLICT(key,id,source_sha256) DO UPDATE SET attempts=attempts+1,
                reason=excluded.reason,last_seen=excluded.last_seen,retry_at=excluded.retry_at,
                resolved_at=NULL,job=excluded.job''',(identity(),identifier,source_hash,reason,when,when,retry,job))
            db.execute('INSERT INTO policy_events VALUES (?,?,?,?,?,?,?)',
                       (identity(),identifier,source_hash,'rejected',reason,when,job))

    def advance(self,identifier,result,*,retry=False,job=''):
        with self.db.transaction(attempts=6) as db:
            if result in ('updated','reused','external_current'):
                scope='key IN (SELECT key FROM policy_scopes WHERE index_name=? AND model=?)'
                pending=db.execute('SELECT 1 FROM policy_failures WHERE id=? AND resolved_at IS NULL AND '+scope+' LIMIT 1',(identifier,index_identity(),gemma.MODEL)).fetchone()
                if pending:
                    db.execute('UPDATE policy_failures SET resolved_at=? WHERE id=? AND resolved_at IS NULL AND '+scope,(time.time(),identifier,index_identity(),gemma.MODEL))
                    db.execute('INSERT INTO policy_events VALUES (?,?,NULL,?,NULL,?,?)',
                               (identity(),identifier,'resolved',time.time(),job))
            db.execute('''UPDATE policy_cursors SET cursor=CASE WHEN ? THEN cursor ELSE ? END,
                scanned=scanned+1,updated=updated+?,reused=reused+?,failed=failed+?,skipped=skipped+?
                WHERE key=?''',(retry,identifier,result=='updated',result=='reused',result=='failed',
                                result not in ('updated','reused','failed'),identity()))
            db.execute('DELETE FROM policy_inflight WHERE key=?',(identity(),))

    def exhausted(self):
        with self.db.transaction(attempts=6) as db:
            db.execute("UPDATE policy_cursors SET cursor='',next_at=? WHERE key=?",(time.time()+3600,identity()))

    def enrichment_progress(self, version, *, cursor=None, phase=None, next_at=0, result=None, reason=None):
        key=identity()+':'+version
        with self.db.transaction(attempts=6) as db:
            db.execute('INSERT OR IGNORE INTO policy_enrichment(key) VALUES (?)',(key,))
            if result:
                db.execute('''UPDATE policy_enrichment SET cursor=?,
                    updated=updated+?,reused=reused+?,failed=failed+?,skipped=skipped+?,
                    last_reason=COALESCE(?,last_reason) WHERE key=?''',
                    (cursor,*(result==counter for counter in COUNTERS),reason,key))
            if phase:
                db.execute('''UPDATE policy_enrichment SET phase=?,next_at=?,cursor=COALESCE(?,cursor)
                    WHERE key=?''',(phase,next_at,cursor,key))
            row=db.execute('''SELECT cursor,updated,reused,failed,skipped,phase,next_at,last_reason
                FROM policy_enrichment WHERE key=?''',(key,)).fetchone()
            return dict(zip(('cursor',*COUNTERS,'phase','next_at','last_reason'),row,strict=True))


class MaintenanceServices(Services):
    def request(self,method,path,body=None,limit=32*1024*1024,*,ndjson=False):
        content = body if ndjson or body is None else vector_json(body)
        content_type = 'application/x-ndjson' if ndjson else 'application/json'
        try:
            with self.client.stream(method,self.es_url+path,content=content,
                                    headers={'Content-Type':content_type}) as response:
                if response.status_code==409:raise SourceError('concurrent_update')
                if response.status_code in (401,403):raise ServiceError('authentication')
                if response.status_code==404:raise SourceError('record_unavailable')
                if response.status_code!=200:raise ServiceError('elasticsearch_unavailable')
                raw=bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw)>limit:raise SourceError('record_too_large')
                value=json.loads(raw)
                if not isinstance(value,dict):raise ServiceError('response_identity')
                return value
        except (httpx.HTTPError,json.JSONDecodeError):raise ServiceError('elasticsearch_unavailable') from None

    @staticmethod
    def current_policy():
        website={'bool':{'filter':[{'prefix':{'id':'websites/'}}],
            'should':[{'bool':{'must_not':[{'exists':{'field':'mime_type'}}]}},{'term':{'mime_type':''}},
                      {'prefix':{'mime_type':'text/'}},{'terms':{'mime_type':['application/xhtml+xml','application/xml','application/json']}}],
            'minimum_should_match':1}}
        return {'bool':{'filter':[{'term':{'text_chunking_version':passages.VERSION}},
                         {'bool':{'should':[{'bool':{'must_not':[website]}},
                          {'term':{'text_extraction_version':WEBSITE_EXTRACTION_VERSION}}],'minimum_should_match':1}}]}}

    def select(self,cursor):
        return self.select_ids(cursor, filter_by=[], exclude=[self.current_policy()])

    def select_enrichment(self,cursor,*,collection=None,limit=100,exclude_newsgroups=()):
        if collection is not None and collection not in COLLECTIONS:
            raise ValueError('Unknown enrichment collection')
        acceptable={'bool':{'should':[{'bool':{'must_not':[{'exists':{'field':'llm_search_enrichment_version'}}]}},
                    {'term':{'llm_search_enrichment_version':search_enrichment.VERSION}}], 'minimum_should_match':1}}
        policy=self.current_policy() if collection in (None,'websites') else {'term':{'text_chunking_version':passages.VERSION}}
        filters=[policy,acceptable,
                 {'nested':{'path':'text','query':{'exists':{'field':'text.text_chunk'}},'score_mode':'none'}}]
        exclude = [current_enrichment()]
        if exclude_newsgroups:
            if (collection != 'newsgroups' or len(exclude_newsgroups) > MAX_HELD_NEWSGROUPS
                    or any(not re.fullmatch(NEWSGROUP_NAME, group) for group in exclude_newsgroups)):
                raise ValueError('Invalid enrichment newsgroup exclusion')
            for group in exclude_newsgroups:
                literal = group.replace('.', r'\.').replace('+', r'\+')
                pattern = f'newsgroups/{literal}{NEWSGROUP_MESSAGE_SUFFIX}'
                exclude.append({'regexp': {'id': {'value': pattern, 'flags': 'NONE'}}})
        return self.select_ids(cursor, filter_by=filters, exclude=exclude,limit=limit,collection=collection)

    def select_ids(self,cursor,*,filter_by,exclude,limit=100,collection=None):
        if type(limit) is not int or not 1<=limit<=(FRONTIER_PAGE_SIZE if collection else 100):
            raise ValueError('Invalid policy selection bound')
        scope={'prefix':{'id':collection+'/'}} if collection else {'exists':{'field':'id'}}
        query={'bool':{'filter':[{'term':{'embedding_model':gemma.MODEL}},{'term':{'file_type':'text'}},
            scope,*filter_by], 'must_not':exclude}}
        if cursor:query['bool']['filter'].append({'range':{'id':{'gt':cursor}}})
        path='/'+quote(self.index,safe='')+'/_search'
        body={'size':limit,'_source':False,'sort':[{'id':'asc'}],'query':query}
        if collection:
            # Refill consumes the ordered IDs only. Keep partial pages out of
            # durable cursor advancement and skip counting all the matches.
            path+='?allow_partial_search_results=false'
            body['track_total_hits']=False
        result=self.request('POST',path,body,limit=(4 if collection else 1)*1024*1024)
        try:
            timed_out = result.get('timed_out')
            failed_shards = result.get('_shards', {}).get('failed')
            if timed_out or failed_shards:
                raise ServiceError('elasticsearch_unavailable')
            if collection and (timed_out is not False or type(failed_shards) is not int):
                raise ValueError()
            hits=result['hits']['hits'];identifiers=[hit['_id'] for hit in hits]
            if (not isinstance(hits,list) or len(hits)>limit or any(not isinstance(value,str) or value<=cursor for value in identifiers)
                    or any(hit.get('_index')!=self.index for hit in hits)
                    or (collection and any(not identifier.startswith(collection+'/') for identifier in identifiers))
                    or identifiers!=sorted(set(identifiers))):raise ValueError()
            return identifiers
        except ServiceError:raise
        except (AttributeError,KeyError,TypeError,ValueError):raise ServiceError('response_identity') from None

    def get(self,identifier):
        result=self.request('GET','/'+quote(self.index,safe='')+'/_doc/'+quote(identifier,safe='')+'?'+
                            urlencode({'_source_includes':','.join(FIELDS)}))
        if (result.get('_id')!=identifier or result.get('_index')!=self.index or not isinstance(result.get('_source'),dict) or result['_source'].get('id')!=identifier
                or type(result.get('_seq_no')) is not int or type(result.get('_primary_term')) is not int):
            raise ServiceError('response_identity')
        if result['_seq_no']<0 or result['_primary_term']<1:raise ServiceError('response_identity')
        return result

    def update(self,hit,patch):
        result=self.request('POST','/'+quote(self.index,safe='')+'/_update/'+quote(hit['_id'],safe='')+'?'+
                            urlencode({'if_seq_no':hit['_seq_no'],'if_primary_term':hit['_primary_term']}),{'doc':patch})
        if result.get('_id')!=hit['_id'] or result.get('_index')!=self.index or result.get('result') not in ('updated','noop'):
            raise ServiceError('update_not_acknowledged')

    def update_many(self, updates):
        """Return one verified result per optimistic partial update in input order."""
        if not 1 <= len(updates) <= UPDATE_BATCH_SIZE:
            raise ValueError('Invalid maintenance update batch size')
        identifiers, lines = [], []
        for hit, patch in updates:
            identifier, seq_no, primary_term = hit.get('_id'), hit.get('_seq_no'), hit.get('_primary_term')
            if (hit.get('_index') != self.index or not isinstance(identifier, str) or not identifier
                    or identifier in identifiers or type(seq_no) is not int or seq_no < 0
                    or type(primary_term) is not int or primary_term < 1):
                raise ServiceError('response_identity')
            identifiers.append(identifier)
            lines.extend((vector_json({'update': {'_id': identifier, 'if_seq_no': seq_no,
                                                'if_primary_term': primary_term}}),
                          vector_json({'doc': patch})))
        body = b'\n'.join((*lines, b''))
        if len(body) > UPDATE_BATCH_BYTES:
            raise ServiceError('update_batch_too_large')
        try:
            result = self.request('POST', '/'+quote(self.index, safe='')+'/_bulk', body,
                                  limit=UPDATE_BATCH_BYTES, ndjson=True)
        except SourceError:
            # Whole-request failures cannot identify a conflicting or missing document.
            raise ServiceError('elasticsearch_unavailable') from None

        outcomes = []
        try:
            if not isinstance(result['items'], list) or type(result['errors']) is not bool:
                raise ValueError()
            for identifier, item in zip(identifiers, result['items'], strict=True):
                if set(item) != {'update'}:
                    raise ValueError()
                row = item['update']
                status = row['status']
                if row['_id'] != identifier or row['_index'] != self.index or type(status) is not int:
                    raise ValueError()
                if status == 200:
                    if row.get('result') not in ('updated', 'noop') or 'error' in row:
                        raise ValueError()
                    outcomes.append(None)
                    continue
                error = row.get('error', {}).get('type')
                if status == 409 and error == 'version_conflict_engine_exception':
                    outcomes.append(SourceError('concurrent_update'))
                elif status == 404 and error == 'document_missing_exception':
                    outcomes.append(SourceError('record_unavailable'))
                else:
                    category = 'authentication' if status in (401, 403) else 'elasticsearch_unavailable'
                    outcomes.append(ServiceError(category))
            if result['errors'] != any(error is not None for error in outcomes):
                raise ValueError()
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ServiceError('update_not_acknowledged') from None
        return outcomes

    def stored_vectors(self,hit):
        rows=hit['_source'].get('text') or []
        if len(rows)>1024:return []  # Bound optional reuse reads, independently of source size.
        try:
            result=self.request('GET','/'+quote(self.index,safe='')+'/_doc/'+quote(hit['_id'],safe='')+'?'+
                urlencode({'_source_includes':'id,embedding_model,text.vector'}))
        except SourceError as error:
            if error.reason=='record_too_large':return []
            raise
        if (result.get('_id')!=hit['_id'] or result.get('_index')!=self.index
                or not isinstance(result.get('_source'),dict) or result['_source'].get('id')!=hit['_id']
                or result['_source'].get('embedding_model')!=gemma.MODEL
                or type(result.get('_seq_no')) is not int or type(result.get('_primary_term')) is not int):
            raise ServiceError('response_identity')
        if (result['_seq_no'],result['_primary_term'])!=(hit['_seq_no'],hit['_primary_term']):
            raise SourceError('concurrent_update')
        saved=result['_source'].get('text')
        if not isinstance(saved,list) or len(saved)!=len(rows):return []
        return [row.get('vector') if isinstance(row,dict) else None for row in saved]

    def vectors(self,inputs,check):
        def infer(values):
            try:response=self.embedder.post(self.embedding_url+'/v1/embeddings',json={
                'model':self.model,'input':values,'encoding_format':'base64'})
            except httpx.HTTPError:raise ServiceError('embedding_unavailable') from None
            if response.status_code!=200:raise ServiceError('embedding_unavailable')
            try:
                return decode_vectors(response.json(),len(values),gemma.MODEL)
            except (ValueError,KeyError,TypeError):raise ServiceError('vector_validation') from None
        try:
            return embed_batches(inputs,infer,batch_size=self.embedding_batch_size,
                                 workers=self.embedding_workers,cache=self.embedding_cache,check=check)
        except ValueError as error:
            if isinstance(error,ServiceError):raise
            raise ServiceError('vector_validation') from None


def repair(services,sources,identifier,check):
    hit=services.get(identifier)
    try:return repair_hit(services,sources,hit,check)
    except SourceError as error:
        if not error.source_sha256:
            source=hit['_source']
            error.source_sha256=((source.get('text_passage_provenance') or {}).get('decoding') or {}).get('source_sha256') or source.get('archive_source_sha256')
        raise
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,MemoryError) as error:
        if isinstance(error,ServiceError):raise
        source=hit['_source']
        hashed=((source.get('text_passage_provenance') or {}).get('decoding') or {}).get('source_sha256') or source.get('archive_source_sha256')
        raise SourceError(getattr(error,'reason','source_validation'),hashed) from None


def repair_hit(services,sources,hit,check):
    return apply_prepared(services,hit,prepare(hit,sources),check)


def apply_prepared(services,hit,prepared,check):
    verify_prepared(hit,prepared)
    if prepared.result:return prepared.result
    check();plan=prepared.plan;patch=dict(prepared.patch)
    stats=getattr(services,'policy_stats',None)
    if stats is None:services.policy_stats=stats=Counter()
    reused_count=submitted_count=0
    if not prepared.reuse:
        reusable={}
        desired=set(prepared.requests)
        candidates=[(request,index) for request,index in prepared.reusable if request in desired]
        if candidates:
            saved=services.stored_vectors(hit)
            for request,index in candidates:
                if not 0<=index<len(saved) or not isinstance(saved[index],list):continue
                try:reusable[request]=gemma.vector(saved[index]).copy()
                except (TypeError,ValueError):continue
        missing=[request for request in prepared.requests if request not in reusable]
        vectors=services.vectors(missing,check) if missing else []
        if not isinstance(vectors,list) or len(vectors)!=len(missing) or any(not isinstance(v,list) for v in vectors):
            raise ServiceError('vector_validation')
        try:
            completed={**reusable,**{request:gemma.vector(vector).copy() for request,vector in zip(missing,vectors,strict=True)}}
            ordered=[completed[request] for request in prepared.requests]
        except (ValueError,TypeError,KeyError):raise ServiceError('vector_validation') from None
        patch['text']=[dict(zip(PASSAGE_FIELDS,(p.text,p.title,p.context,p.kind,p.start,p.end)),vector=ordered[pos].copy())
                       for p,pos in zip(plan.passages,prepared.positions,strict=True)]
        reused_count,submitted_count=len(reusable),len(missing)
    check();services.update(hit,patch)
    stats['stored_vectors_reused']+=reused_count;stats['submitted_inputs']+=submitted_count
    stats['documents']+=1
    return 'reused' if prepared.reuse else 'updated'


def foreground(root,*,enrichment=False):
    try:
        path=Path(root)/'indexing-state/policy-control.json'
        if path.stat().st_size>4096:raise ValueError()
        control=json.loads(path.read_text())
        if (type(control['foreground']) is not bool or type(control['enabled']) is not bool
                or type(control['updated_at']) not in (int,float) or not 0<=control['updated_at']<10**12):raise ValueError()
        enabled=control.get('enrichment_enabled',False) if enrichment else control['enabled']
        if type(enabled) is not bool:raise ValueError()
        return (control['foreground'] or not enabled or control.get('key')!=identity()
                or control['updated_at']<time.time()-180)
    except (OSError,ValueError,KeyError,TypeError):raise ServiceError('portal_state_unavailable') from None


def run(root,state,services,sources,job,*,maximum=2000,seconds=600,should_yield=None,clock=time.monotonic,
        pool=None,budget_root=None):
    if not re.fullmatch(r'eqarchives-policy-'+identity()+r'-[1-9][0-9]{0,8}',job) or not 1<=maximum<=2000 or not 1<=seconds<=600:
        raise ValueError('Invalid maintenance bounds')
    deadline=clock()+seconds;processed=0;phase='bounded';error=None;last_report=float('-inf')
    paid=None
    should_yield=should_yield or (lambda:foreground(root))
    def check():
        if clock()>=deadline or should_yield():raise YieldWork()
    def receipt(terminal=False):
        record={**state.snapshot(),'job':job,'policy':passages.VERSION,'model':gemma.MODEL,
                'image':os.environ.get('IMPORT_IMAGE',''),'phase':phase,'error':error,'updated_at':time.time(),'terminal':terminal,
                'execution':{'preparation_workers':pool.workers if pool else 0,**getattr(services,'policy_stats',{})}}
        if paid is not None:
            record['next_at']=min(record['next_at'],paid['next_at'])
            record['enrichment']={k:v for k,v in paid.items() if k!='cursor'}
        save(state.root/(job+'.json'),record)
        return record
    def dispatch(identifier):
        try:
            hit=services.get(identifier)
            try:return hit,pool.submit(hit)
            except WorkerLost as error:return hit,error
        except (SourceError,ServiceError) as error:return None,error
    def item(identifier,retry=False,task=None):
        nonlocal processed,last_report
        check();source_hash=None;hit=None;pending=task[1] if task else None
        try:
            if not retry and state.deferred(identifier):result='deferred'
            else:
                if not state.begin(identifier,job):raise SourceError('repeated_worker_interruption')
                if pool:
                    hit,pending=task or dispatch(identifier)
                    if isinstance(pending,Exception):raise pending
                    prepared=pending.result(check)
                    result=apply_prepared(services,hit,prepared,check)
                else:result=repair(services,sources,identifier,check)
        except (SourceError,UnicodeError,ValueError,TypeError,KeyError,AttributeError,RecursionError,MemoryError) as failure:
            if isinstance(failure,ServiceError):raise
            reason=failure.reason if isinstance(failure,SourceError) else 'source_validation'
            source_hash=getattr(failure,'source_sha256',None)
            if not source_hash and hit:
                source_hash=((hit['_source'].get('text_passage_provenance') or {}).get('decoding') or {}).get('source_sha256') or hit['_source'].get('archive_source_sha256')
            state.reject(identifier,reason,source_hash,job);result='failed'
        finally:
            if pool and pending is not None and not isinstance(pending,Exception):pending.cancel()
        state.advance(identifier,result,retry=retry,job=job);processed+=1
        if clock()-last_report>=5:
            receipt();last_report=clock()
    try:
        interrupted=state.inflight()
        if budget_root and not interrupted:
            paid=run_enrichment(state,services,sources,job,budget_root,check,seconds=min(75,seconds/4))
        due=list(dict.fromkeys(([interrupted] if interrupted else [])+state.due(min(100,maximum))))[:maximum]
        if not due and state.snapshot()['next_at']>time.time():
            phase='sweep_waiting';return receipt(True)
        for identifier in due:
            item(identifier,retry=True)
            if processed>=maximum:break
        while processed<maximum:
            check();batch=services.select(state.snapshot()['cursor'])
            if not batch:
                state.exhausted();phase='sweep_waiting';break
            if not pool:
                for identifier in batch:
                    item(identifier)
                    if processed>=maximum:break
            else:
                remaining=iter(batch[:maximum-processed]);pending=deque();exhausted=False
                while pending or not exhausted:
                    while len(pending)<pool.workers and not exhausted:
                        check()
                        identifier=next(remaining,None)
                        if identifier is None:exhausted=True;break
                        pending.append((identifier,None if state.deferred(identifier) else dispatch(identifier)))
                    if pending:
                        identifier,task=pending.popleft();item(identifier,task=task)
    except YieldWork:
        state.abandon();phase='yielded'
    except WorkerLost:
        # Per-process pipes identify the exact crashing record. Retain only its
        # inflight marker; a subsequent slice isolates it before prefetching.
        phase='retry_wait';error='Preparation worker stopped; the exact record will retry from its saved checkpoint.'
    except (ServiceError,OSError,sqlite3.Error):
        state.abandon()
        phase='retry_wait';error='Background policy repair paused; verified progress is retained.'
    finally:
        if pool:pool.cancel_pending()
    return receipt(True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/data'))
    args=parser.parse_args();state=None;services=None;sources=None;pool=None
    try:
        state=State('/indexing-state');services=MaintenanceServices()
        sources=Sources(['/archive-sources','/archive'],'/data/publisher.git')
        workers=int(os.environ.get('POLICY_PREPARATION_WORKERS','0'))
        if not 0<=workers<=4:raise ValueError('Invalid preparation worker count')
        if workers:
            gemma.load_tokenizer()
            renderer_identity();request_identity()
            pool=PreparationPool(sources.roots,sources.publication,workers)
        options={'pool':pool} if pool else {}
        if budget_root:=os.environ.get('LUNA_DAILY_BUDGET_ROOT'):
            options['budget_root']=budget_root
        result=run(args.root,state,services,sources,os.environ['POLICY_JOB_NAME'],**options)
        print(json.dumps(result),flush=True)
        return 1 if result['phase']=='retry_wait' else 0
    finally:
        if pool:pool.close()
        if sources:sources.close()
        if services:services.close()
        if state:state.close()


if __name__=='__main__':raise SystemExit(main())
