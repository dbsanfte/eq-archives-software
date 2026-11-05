"""Persistent, source-bound rejection ledger for the broad text backfill.

This is diagnostic state, not an embedding/extraction completion checkpoint.
No upstream payload, source text, credentials or exception traceback is stored.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .sqlite_state import SQLiteState


def source_hash(source,repo):
    root=Path(repo).resolve();path=(root/source['id']).resolve()
    if root not in path.parents:return None
    try:
        if path.stat().st_size>64*1024*1024:return None
        digest=hashlib.sha256();remaining=64*1024*1024+1
        with path.open('rb') as stream:
            while chunk:=stream.read(min(1024*1024,remaining)):
                digest.update(chunk);remaining-=len(chunk)
                if not remaining:return None
        return digest.hexdigest()
    except OSError:return None


def reason(error):
    name=type(error).__name__
    if isinstance(error,UnicodeError) or isinstance(error,ValueError) and str(error).startswith('Could not decode file '):return 'source_decoding_rejected'
    if isinstance(error,FileNotFoundError):return 'source_unavailable'
    if name=='ConflictError':return 'concurrent_update'
    if name in ('AuthenticationError','PermissionDeniedError'):return 'upstream_authentication'
    if name in ('APIConnectionError','APITimeoutError','ConnectionTimeout','ConnectionError','InternalServerError','RateLimitError'):
        return 'upstream_transport'
    return 'source_or_vector_validation'


class Ledger:
    def __init__(self,path):
        self.state=SQLiteState(path,durable=True)
        with self.state.transaction(attempts=6) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS exceptions (
                id TEXT NOT NULL, model TEXT NOT NULL, policy TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                reason TEXT NOT NULL, exception_type TEXT NOT NULL, first_seen REAL NOT NULL,
                last_seen REAL NOT NULL, attempts INTEGER NOT NULL, job TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','resolved')), resolved_at REAL,
                resolution_policy TEXT, resolution_source_sha256 TEXT, resolution_job TEXT,
                PRIMARY KEY(id,model,policy,source_sha256))''')
            db.execute('CREATE INDEX IF NOT EXISTS exception_status ON exceptions(status,id)')
        self._pending=None;self._synced=0;self.sync()

    def sync(self):
        rows=self.state.connection().execute("SELECT DISTINCT id FROM exceptions WHERE status='pending' LIMIT 10001").fetchall()
        self._pending={r[0] for r in rows} if len(rows)<=10000 else None;self._synced=time.monotonic()

    def has_pending(self,record_id):
        if time.monotonic()-self._synced>=60:self.sync()
        if self._pending is not None:return record_id in self._pending
        return self.state.connection().execute("SELECT 1 FROM exceptions WHERE status='pending' AND id=? LIMIT 1",(record_id,)).fetchone() is not None

    def record(self,record_id,*,model,policy,source_sha256=None,error=None,category=None,job=''):
        if source_sha256 is not None and not re.fullmatch('[0-9a-f]{64}',source_sha256):raise ValueError('Invalid diagnostic source hash')
        if category not in (None,'source_unavailable'):raise ValueError('Invalid diagnostic reason')
        now=time.time();category=category or reason(error)
        with self.state.transaction(attempts=6) as db:
            db.execute('''INSERT INTO exceptions VALUES (?,?,?,?,?,?,?,?,?,?, 'pending',NULL,NULL,NULL,NULL)
                ON CONFLICT(id,model,policy,source_sha256) DO UPDATE SET
                reason=excluded.reason,exception_type=excluded.exception_type,last_seen=excluded.last_seen,
                attempts=exceptions.attempts+1,job=excluded.job,status='pending',resolved_at=NULL,
                resolution_policy=NULL,resolution_source_sha256=NULL,resolution_job=NULL''',
                (record_id,model,policy,source_sha256 or '',category,type(error).__name__ if error else '',now,now,1,job[:253]))
        if self._pending is not None:
            self._pending.add(record_id)
            if len(self._pending)>10000:self._pending=None

    def resolve(self,record_id,*,policy,source_sha256=None,job=''):
        if not self.has_pending(record_id):return
        with self.state.transaction(attempts=6) as db:
            db.execute("UPDATE exceptions SET status='resolved',resolved_at=?,resolution_policy=?,resolution_source_sha256=?,resolution_job=? WHERE id=? AND status='pending'",
                       (time.time(),policy,source_sha256,job[:253],record_id))
        if self._pending is not None:self._pending.discard(record_id)

    def pending_ids(self,limit=1000):
        if not 1<=limit<=1000:raise ValueError('Use a bounded diagnostic retry batch of 1–1000 records')
        return [r[0] for r in self.state.connection().execute("SELECT DISTINCT id FROM exceptions WHERE status='pending' ORDER BY id LIMIT ?",(limit,)).fetchall()]

    def report(self,*,limit=100,status='pending'):
        if not 1<=limit<=1000 or status not in ('pending','resolved','all'):raise ValueError('Invalid diagnostic report bounds')
        db=self.state.connection();summary={state:count for state,count in db.execute('SELECT status,COUNT(*) FROM exceptions GROUP BY status')}
        cursor=db.execute('SELECT * FROM exceptions '+("WHERE status=? " if status!='all' else '')+'ORDER BY last_seen DESC,id LIMIT ?',
                          (status,limit) if status!='all' else (limit,))
        columns=[v[0] for v in cursor.description]
        return {'summary':summary,'records':[dict(zip(columns,row)) for row in cursor.fetchall()],'limit':limit}

    def close(self):self.state.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path',type=Path,default=os.environ.get('REINDEX_DIAGNOSTICS_PATH'))
    parser.add_argument('--status',choices=('pending','resolved','all'),default='pending')
    parser.add_argument('--limit',type=int,default=100)
    args=parser.parse_args()
    if not args.path:parser.error('Set REINDEX_DIAGNOSTICS_PATH or --path')
    ledger=Ledger(args.path)
    try:print(json.dumps(ledger.report(limit=args.limit,status=args.status),indent=2))
    finally:ledger.close()


if __name__=='__main__':main()
