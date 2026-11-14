"""Bounded, model-bound float32 cache with decaying heat and idle expiry.

No source text or credentials are stored. Cache misses and temporary storage
failures use inference; invalid inference responses never become cache entries.
"""
from collections import Counter
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import time

from . import gemma
from .sqlite_state import SQLiteState

LOG=logging.getLogger(__name__)
DEFAULT_BYTES=512*1024*1024
DEFAULT_ENTRIES=100000
DEFAULT_HALF_LIFE=86400
DEFAULT_IDLE=7*86400
VECTOR_BYTES=768*4
WAL_LIMIT=8*1024*1024


def runtime_identity():
    configured=os.environ.get('EMBEDDING_CACHE_RUNTIME')
    paths=[Path(configured)] if configured else [Path('/opt/eqarchives/embedding-runtime.env')]+[
        p/'k8s-manifests/embeddings/runtime.env' for p in Path(__file__).resolve().parents]
    path=next((p for p in paths if p.is_file()),None)
    if path is None:raise ValueError('Pinned embedding runtime is unavailable for cache identity')
    settings=dict(line.split('=',1) for line in path.read_text().splitlines() if re.match(r'^[A-Z][A-Z0-9_]*=',line))
    cuda_configured=os.environ.get('EMBEDDING_CACHE_CUDA_RUNTIME')
    cuda_paths=[Path(cuda_configured)] if cuda_configured else [Path('/opt/eqarchives/embedding-cuda.env')]+[
        p/'scripts/cuda-batch-server/deployment.env' for p in Path(__file__).resolve().parents]
    cuda_path=next((p for p in cuda_paths if p.is_file()),None)
    runtime=os.environ.get('EMBEDDING_CACHE_BACKEND_IMAGE')
    if runtime is None:
        if cuda_path is None:raise ValueError('Pinned CUDA runtime is unavailable for cache identity')
        cuda=dict(line.split('=',1) for line in cuda_path.read_text().splitlines() if re.match(r'^[A-Z][A-Z0-9_]*=',line))
        runtime=cuda.get('CUDA_IMAGE','')
    if (settings.get('MODEL_ALIAS')!=gemma.MODEL or not re.fullmatch('[0-9a-f]{64}',settings.get('MODEL_SHA256',''))
            or not re.fullmatch(r'.+@sha256:[0-9a-f]{64}',runtime)):
        raise ValueError('Invalid pinned embedding cache identity')
    return json.dumps({'model':gemma.MODEL,'weights':settings['MODEL_SHA256'],
                       'runtime':runtime,'tokenizer':gemma.TOKENIZER_SHA256,
                       'dimensions':768,'format':'float32-v1'},sort_keys=True)


def packed_vector(value):
    values=gemma.vector([float(v) for v in value])
    raw=struct.pack('<768f',*values)
    gemma.vector(list(struct.unpack('<768f',raw)))
    return raw


def optional_cache():
    try:return EmbeddingCache.from_environment()
    except (ValueError,OSError,OverflowError) as error:
        LOG.warning('Embedding cache configuration unavailable (%s); using inference',type(error).__name__)
        return None


class EmbeddingCache:
    def __init__(self,path,namespace,*,max_bytes=DEFAULT_BYTES,max_entries=DEFAULT_ENTRIES,
                 half_life=DEFAULT_HALF_LIFE,max_idle=DEFAULT_IDLE,clock=time.time):
        if (type(max_bytes) is not int or max_bytes<131072 or type(max_entries) is not int or max_entries<1
                or not math.isfinite(half_life) or half_life<1 or not math.isfinite(max_idle) or max_idle<1
                or not isinstance(namespace,str) or not namespace):raise ValueError('Invalid embedding cache bounds')
        self.capacity=min(max_entries,(max_bytes-65536)//4224)
        self.state=SQLiteState(path,max_bytes=max_bytes);self.namespace=namespace
        self.half_life=half_life;self.max_idle=max_idle;self.clock=clock
        self.decay=math.log(2)/half_life;self.initialized=False;self.stats=Counter();self.warned_at=None

    @classmethod
    def from_environment(cls):
        path=os.environ.get('EMBEDDING_CACHE_PATH')
        if not path:return None
        return cls(path,runtime_identity(),max_bytes=int(os.environ.get('EMBEDDING_CACHE_MAX_BYTES',DEFAULT_BYTES)),
                   max_entries=int(os.environ.get('EMBEDDING_CACHE_MAX_ENTRIES',DEFAULT_ENTRIES)),
                   half_life=float(os.environ.get('EMBEDDING_CACHE_HALF_LIFE_SECONDS',DEFAULT_HALF_LIFE)),
                   max_idle=float(os.environ.get('EMBEDDING_CACHE_MAX_IDLE_SECONDS',DEFAULT_IDLE)))

    def key(self,request):
        return hashlib.sha256((self.namespace+'\0'+request).encode()).hexdigest()

    def initialize(self):
        if self.initialized:return
        with self.state.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cache_policy (id INTEGER PRIMARY KEY CHECK(id=1), half_life REAL NOT NULL)')
            db.execute('INSERT OR IGNORE INTO cache_policy VALUES (1,?)',(self.half_life,))
            if db.execute('SELECT half_life FROM cache_policy').fetchone()[0]!=self.half_life:
                raise ValueError('Cache heat policy changed; use a fresh cache path for all clients')
            db.execute('''CREATE TABLE IF NOT EXISTS embeddings (
                key TEXT PRIMARY KEY, vector BLOB NOT NULL, checksum BLOB NOT NULL,
                heat REAL NOT NULL, last_hit REAL NOT NULL, priority REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS embedding_priority ON embeddings(priority,last_hit)')
            db.execute('CREATE INDEX IF NOT EXISTS embedding_idle ON embeddings(last_hit)')
            db.execute('CREATE TABLE IF NOT EXISTS cache_totals (id INTEGER PRIMARY KEY CHECK(id=1), entries INTEGER NOT NULL)')
            if db.execute('SELECT 1 FROM cache_totals WHERE id=1').fetchone() is None:
                db.execute('INSERT INTO cache_totals SELECT 1,COUNT(*) FROM embeddings')
            db.execute('''CREATE TRIGGER IF NOT EXISTS embedding_insert_count AFTER INSERT ON embeddings
                          BEGIN UPDATE cache_totals SET entries=entries+1 WHERE id=1; END''')
            db.execute('''CREATE TRIGGER IF NOT EXISTS embedding_delete_count AFTER DELETE ON embeddings
                          BEGIN UPDATE cache_totals SET entries=entries-1 WHERE id=1; END''')
        self.initialized=True

    def unavailable(self,error):
        self.stats['unavailable']+=1;now=self.clock()
        if self.warned_at is None or now-self.warned_at>=60:
            LOG.warning('Embedding cache unavailable (%s); using inference without cached results',type(error).__name__)
            self.warned_at=now

    def journal_budget(self):
        # Long-lived external readers must not let WAL files grow without
        # limit. Checkpoint outside transactions, or bypass the cache until
        # the reader releases its snapshot. Never wait on network work here.
        wal=Path(str(self.state.path)+'-wal')
        if wal.exists() and wal.stat().st_size>=WAL_LIMIT:
            busy,_,_=self.state.connection().execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if busy:raise ValueError('Cache journal budget is waiting for a reader')

    def get_many(self,requests):
        if not requests:return {}
        if len(requests)>64:raise ValueError('Cache lookup exceeds embedding batch capacity')
        result={};now=self.clock()
        try:
            self.initialize()
            self.journal_budget()
            with self.state.transaction() as db:
                for request in dict.fromkeys(requests):
                    key=self.key(request)
                    row=db.execute('SELECT vector,checksum,heat,last_hit FROM embeddings WHERE key=?',(key,)).fetchone()
                    if row is None:continue
                    raw,checksum,heat,last=row
                    try:
                        if not math.isfinite(heat) or heat<1 or not math.isfinite(last):raise ValueError('Corrupt cached heat')
                        if now-last>=self.max_idle:
                            db.execute('DELETE FROM embeddings WHERE key=?',(key,));self.stats['expired']+=1;continue
                        if len(raw)!=VECTOR_BYTES or hashlib.sha256(raw).digest()!=checksum:raise ValueError('Corrupt cached vector')
                        vector=gemma.vector(list(struct.unpack('<768f',raw)))
                    except (ValueError,TypeError,struct.error):
                        db.execute('DELETE FROM embeddings WHERE key=?',(key,));self.stats['corrupt']+=1;continue
                    heat=heat*math.exp(-max(0,now-last)*self.decay)+1
                    db.execute('UPDATE embeddings SET heat=?,last_hit=?,priority=? WHERE key=?',
                               (heat,now,math.log(heat)+now*self.decay,key))
                    result[request]=vector
        except (sqlite3.Error,OSError,ValueError) as error:
            self.unavailable(error);result={}
        self.stats['hits']+=len(result);self.stats['misses']+=len(set(requests))-len(result)
        return result

    def put_many(self,requests,vectors):
        if len(requests)!=len(vectors) or len(requests)>64:raise ValueError('Invalid embedding cache batch')
        # Validate the COMPLETE response before entering a cache transaction.
        rows=[(self.key(request),packed_vector(vector)) for request,vector in zip(requests,vectors,strict=True)]
        if not rows:return
        now=self.clock()
        try:
            self.initialize()
            self.journal_budget()
            with self.state.transaction() as db:
                expired=db.execute('DELETE FROM embeddings WHERE key IN (SELECT key FROM embeddings WHERE last_hit<=? LIMIT 128)',(now-self.max_idle,)).rowcount
                self.stats['expired']+=expired
                count=db.execute('SELECT entries FROM cache_totals WHERE id=1').fetchone()[0]
                # Reserve headroom in one bounded eviction, indexed by decayed
                # heat. All scores share -now*decay, so that factor cancels.
                additions=sum(db.execute('SELECT 1 FROM embeddings WHERE key=?',(key,)).fetchone() is None for key,_ in rows)
                excess=max(0,count+additions-self.capacity)
                if excess:
                    removed=db.execute('DELETE FROM embeddings WHERE key IN (SELECT key FROM embeddings ORDER BY priority,last_hit LIMIT ?)',(excess,)).rowcount
                    self.stats['evictions']+=removed
                for key,raw in rows[-self.capacity:]:
                    # A simultaneous winner already has validated same-model
                    # content. Do not reset its accumulated heat or last hit.
                    db.execute('INSERT OR IGNORE INTO embeddings VALUES (?,?,?,?,?,?)',
                               (key,raw,hashlib.sha256(raw).digest(),1.,now,now*self.decay))
        except (sqlite3.Error,OSError,ValueError) as error:self.unavailable(error)

    def embed(self,requests,fetch):
        unique=list(dict.fromkeys(requests));cached=self.get_many(unique)
        missing=[request for request in unique if request not in cached]
        if missing:
            values=fetch(missing)
            if len(values)!=len(missing):raise ValueError('Incomplete embedding response cannot enter the cache')
            validated=[list(struct.unpack('<768f',packed_vector(v))) for v in values]
            self.put_many(missing,validated);cached.update(zip(missing,validated,strict=True))
        return [cached[request].copy() for request in requests]

    def close(self):self.state.close()
