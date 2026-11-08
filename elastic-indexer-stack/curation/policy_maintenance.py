"""Bounded policy-only repair, checkpointed between foreground curation imports.

Same model, no paid enrichment, no source preparation, no create/upsert/delete.
Only acknowledged optimistic updates advance repair counts and resolve failures.
"""
import argparse
from dataclasses import replace
import hashlib
import html
import json
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote, urlencode

import httpx
from bs4 import BeautifulSoup

from common import save
from index_captures import Services
from indexer import gemma, passages
from indexer.archive_handler import ArchiveHandler
from indexer.html_extraction import WEBSITE_EXTRACTION_VERSION, website_markdown
from indexer.source_encoding import decode_source, repaired_title
from indexer.sqlite_state import SQLiteState
from indexer.vector_json import dumps as vector_json
from policy_sources import Sources, SourceError, relative

APP='eqarchives-policy-maintenance'
LEGACY_POLICY='embeddinggemma2-paragraphs-480-overlap48-v1'
FIELDS=['id','file_type','mime_type','title','text_full','text.text_chunk','text.title','text.context',
        'text.kind','text.source_start','text.source_end','embedding_model','text_chunking_version',
        'text_extraction_version','text_passage_provenance','archive_source_sha256','archive_commit']


def index_identity():
    return os.environ.get('POLICY_INDEX',os.environ.get('ELASTICSEARCH_INDEX','eq-archive'))


def identity():
    return hashlib.sha256(json.dumps([index_identity(),gemma.MODEL,passages.VERSION,WEBSITE_EXTRACTION_VERSION]).encode()).hexdigest()[:16]


def newer(version):
    if version==LEGACY_POLICY:return False
    old=re.fullmatch(r'eq-passages-480-v(\d+)',version or '')
    current=re.fullmatch(r'eq-passages-480-v(\d+)',passages.VERSION)
    return not old or not current or int(old[1])>int(current[1])


class ServiceError(ValueError):
    pass


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
                key TEXT PRIMARY KEY,index_name TEXT NOT NULL,model TEXT NOT NULL,policy TEXT NOT NULL);'''
            for statement in schema.split(';'):
                if statement.strip():db.execute(statement)
        with self.db.transaction(attempts=6) as db:
            db.execute("INSERT OR IGNORE INTO policy_cursors VALUES (?,'',0,0,0,0,0,0)",(identity(),))
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


class MaintenanceServices(Services):
    def request(self,method,path,body=None,limit=32*1024*1024):
        try:
            with self.client.stream(method,self.es_url+path,content=vector_json(body) if body is not None else None,
                                    headers={'Content-Type':'application/json'}) as response:
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

    def select(self,cursor):
        website={'bool':{'filter':[{'prefix':{'id':'websites/'}}],
            'should':[{'bool':{'must_not':[{'exists':{'field':'mime_type'}}]}},{'term':{'mime_type':''}},
                      {'prefix':{'mime_type':'text/'}},{'terms':{'mime_type':['application/xhtml+xml','application/xml','application/json']}}],
            'minimum_should_match':1}}
        query={'bool':{'filter':[{'term':{'embedding_model':gemma.MODEL}},{'term':{'file_type':'text'}},{'exists':{'field':'id'}}],
                       'must_not':[{'bool':{'filter':[{'term':{'text_chunking_version':passages.VERSION}},
                         {'bool':{'should':[{'bool':{'must_not':[website]}},
                          {'term':{'text_extraction_version':WEBSITE_EXTRACTION_VERSION}}],'minimum_should_match':1}}]}}]}}
        if cursor:query['bool']['filter'].append({'range':{'id':{'gt':cursor}}})
        result=self.request('POST','/'+quote(self.index,safe='')+'/_search',
                            {'size':100,'_source':False,'sort':[{'id':'asc'}],'query':query},limit=1024*1024)
        try:
            hits=result['hits']['hits'];identifiers=[hit['_id'] for hit in hits]
            if (not isinstance(hits,list) or len(hits)>100 or any(not isinstance(value,str) or value<=cursor for value in identifiers)
                    or any(hit.get('_index')!=self.index for hit in hits)
                    or identifiers!=sorted(set(identifiers))):raise ValueError()
            return identifiers
        except (KeyError,TypeError,ValueError):raise ServiceError('response_identity') from None

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

    def vectors(self,inputs,check):
        def infer(values):
            check()
            try:response=self.embedder.post(self.embedding_url+'/v1/embeddings',json={
                'model':self.model,'input':values,'encoding_format':'float'})
            except httpx.HTTPError:raise ServiceError('embedding_unavailable') from None
            if response.status_code!=200:raise ServiceError('embedding_unavailable')
            try:
                payload=response.json();rows=payload['data']
                if (payload.get('model')!=gemma.MODEL or len(rows)!=len(values)
                        or any(type(row.get('index')) is not int for row in rows)
                        or sorted(row['index'] for row in rows)!=list(range(len(values)))):
                    raise ValueError()
                return [gemma.vector(row['embedding']) for row in sorted(rows,key=lambda r:r['index'])]
            except (ValueError,KeyError,TypeError):raise ServiceError('vector_validation') from None
        result=[]
        for offset in range(0,len(inputs),8):
            check();batch=inputs[offset:offset+8]
            result.extend(self.embedding_cache.embed(batch,infer) if self.embedding_cache else infer(batch))
        if len(result)!=len(inputs):raise ServiceError('vector_validation')
        return [gemma.vector(v) for v in result]


def repair(services,sources,identifier,check):
    hit=services.get(identifier)
    try:return repair_hit(services,sources,hit,check)
    except SourceError as error:
        if not error.source_sha256:
            source=hit['_source']
            error.source_sha256=((source.get('text_passage_provenance') or {}).get('decoding') or {}).get('source_sha256') or source.get('archive_source_sha256')
        raise


def repair_hit(services,sources,hit,check):
    source=hit['_source'];identifier=hit['_id'];reader=source.get('text_full') or ''
    previous=source.get('text_passage_provenance') or {};version=source.get('text_chunking_version') or ''
    if source.get('embedding_model')!=gemma.MODEL or source.get('file_type')!='text':return 'different_model'
    if version and newer(version):return 'newer_policy'
    relative(identifier)
    website=identifier.startswith('websites/') and source.get('file_type')=='text'
    mime=source.get('mime_type') or ''
    textual=not mime or mime.startswith('text/') or mime in ('application/xhtml+xml','application/xml','application/json')
    if not reader.strip():raise SourceError('reader_unavailable')
    bindings=previous.get('sources') or [{k:v for k,v in previous.items() if k!='decoding'}]
    legacy_reader=version==LEGACY_POLICY and not previous and website and textual
    if not legacy_reader and (len(bindings)!=1 or bindings[0].get('source_sha256')!=passages.digest(reader)):
        raise SourceError('reader_integrity')
    complete=version==passages.VERSION and previous.get('version')==passages.VERSION and (not website or not textual or source.get('text_extraction_version')==WEBSITE_EXTRACTION_VERSION)
    patch={};raw=None;decoding=None
    if website and textual:
        data=sources.read(source);hint=(previous.get('decoding') or {}).get('encoding')
        decoded=decode_source(data,mime,detection={'encoding':hint,'confidence':1.0} if hint else None)
        decoded=replace(decoded,text=decoded.text.replace('\r\n','\n').replace('\r','\n'))
        raw=decoded.text;decoding=decoded.provenance()
        title=BeautifulSoup(raw,'html.parser').title
        corrected=repaired_title(source.get('title'),title.string if title else None,decoded)
        if corrected!=source.get('title'):patch['title']=corrected
        if legacy_reader or source.get('text_extraction_version')!=WEBSITE_EXTRACTION_VERSION:
            archive=ArchiveHandler();url=archive._convert_to_archive_url(identifier)
            alternate=archive._strip_index_html_from_url(url)
            header='<b>Page URL:</b> '+html.escape(url)+'<br/>'
            if alternate and alternate!=url:header+='<b>Alternate Page URL:</b> '+html.escape(alternate)+'<br/>'
            reader=website_markdown(header+'<hr/>'+raw)
            if not reader.strip():raise SourceError('reader_integrity')
            if reader!=source['text_full']:patch['text_full']=reader
            patch['text_extraction_version']=WEBSITE_EXTRACTION_VERSION
    plan=passages.prepare(reader,identifier,patch.get('title',source.get('title')) or '',html_source=raw,mime_type=mime)
    passages.verify(reader,plan)
    fields=('text_chunk','title','context','kind','source_start','source_end')
    expected=[(p.text,p.title,p.context,p.kind,p.start,p.end) for p in plan.passages]
    saved=[tuple(row.get(k) for k in fields) for row in source.get('text') or []]
    comparable=lambda value:{k:v for k,v in value.items() if k!='version'}
    reuse=(not legacy_reader and reader==source.get('text_full') and expected==saved and comparable(bindings[0])==comparable(plan.provenance()))
    if complete and not patch:
        if not reuse:raise SourceError('checkpoint_integrity')
        return 'external_current'
    provenance={'version':passages.VERSION,'sources':[plan.provenance()]}
    if decoding:provenance['decoding']=decoding
    patch.update(text_chunking_version=passages.VERSION,text_passage_provenance=provenance)
    if not reuse:
        unique,positions=passages.embedding_inputs(plan)
        vectors=services.vectors([p.request for p in unique],check)
        patch['text']=[dict(zip(fields,(p.text,p.title,p.context,p.kind,p.start,p.end)),vector=vectors[pos])
                       for p,pos in zip(plan.passages,positions,strict=True)]
    if plan.method=='archive_status' or 'supporting_asset' in plan.exclusions:
        patch.update(llm_summary_vector=None,llm_image_text_vector=None)
    check();services.update(hit,patch)
    return 'reused' if reuse else 'updated'


def foreground(root):
    try:
        path=Path(root)/'indexing-state/policy-control.json'
        if path.stat().st_size>4096:raise ValueError()
        control=json.loads(path.read_text())
        if (type(control['foreground']) is not bool or type(control['enabled']) is not bool
                or type(control['updated_at']) not in (int,float) or not 0<=control['updated_at']<10**12):raise ValueError()
        return (control['foreground'] or not control['enabled'] or control.get('key')!=identity()
                or control['updated_at']<time.time()-180)
    except (OSError,ValueError,KeyError,TypeError):raise ServiceError('portal_state_unavailable') from None


def run(root,state,services,sources,job,*,maximum=2000,seconds=600,should_yield=None,clock=time.monotonic):
    if not re.fullmatch(r'eqarchives-policy-'+identity()+r'-[1-9][0-9]{0,8}',job) or not 1<=maximum<=2000 or not 1<=seconds<=600:
        raise ValueError('Invalid maintenance bounds')
    deadline=clock()+seconds;processed=0;phase='bounded';error=None;last_report=float('-inf')
    should_yield=should_yield or (lambda:foreground(root))
    def check():
        if clock()>=deadline or should_yield():raise YieldWork()
    def receipt(terminal=False):
        record={**state.snapshot(),'job':job,'policy':passages.VERSION,'model':gemma.MODEL,
                'image':os.environ.get('IMPORT_IMAGE',''),'phase':phase,'error':error,'updated_at':time.time(),'terminal':terminal}
        save(state.root/(job+'.json'),record)
        return record
    def item(identifier,retry=False):
        nonlocal processed,last_report
        check();source_hash=None
        try:
            if not retry and state.deferred(identifier):result='deferred'
            else:
                if not state.begin(identifier,job):raise SourceError('repeated_worker_interruption')
                result=repair(services,sources,identifier,check)
        except (SourceError,UnicodeError,ValueError,TypeError,KeyError,AttributeError,RecursionError,MemoryError) as failure:
            if isinstance(failure,ServiceError):raise
            reason=failure.reason if isinstance(failure,SourceError) else 'source_validation'
            source_hash=getattr(failure,'source_sha256',None)
            state.reject(identifier,reason,source_hash,job);result='failed'
        state.advance(identifier,result,retry=retry,job=job);processed+=1
        if clock()-last_report>=5:
            receipt();last_report=clock()
    try:
        interrupted=state.inflight()
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
            for identifier in batch:
                item(identifier)
                if processed>=maximum:break
    except YieldWork:
        state.abandon();phase='yielded'
    except (ServiceError,OSError,sqlite3.Error):
        state.abandon()
        phase='retry_wait';error='Background policy repair paused; verified progress is retained.'
    return receipt(True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/data'))
    args=parser.parse_args();state=None;services=None;sources=None
    try:
        state=State('/indexing-state');services=MaintenanceServices()
        sources=Sources(['/archive-sources','/archive'],'/data/publisher.git')
        result=run(args.root,state,services,sources,os.environ['POLICY_JOB_NAME'])
        print(json.dumps(result),flush=True)
        return 1 if result['phase']=='retry_wait' else 0
    finally:
        if sources:sources.close()
        if services:services.close()
        if state:state.close()


if __name__=='__main__':raise SystemExit(main())
