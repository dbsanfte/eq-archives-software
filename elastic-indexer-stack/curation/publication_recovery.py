"""Durable retries of the same approved publication; no regenerated approvals."""
from datetime import datetime,timedelta
import json

from common import CrawlError,now
from publication_git import PublicationError
from state import connect,unpack,Lane,active_operation

DELAYS=(300,900,3600)


def register(store,operation,error,*,initial=False):
    previous=store.db.execute('SELECT * FROM publication_retries WHERE operation=?',(operation['id'],)).fetchone()
    if previous and previous['paused']:return
    legacy=initial and (str(error).startswith(('Archive Git fetch failed.','Archive Git operation timed out','Archive publication could not be confirmed','Worker stopped; resume explicitly')))
    retryable=error.retryable if isinstance(error,PublicationError) else legacy
    if not retryable:
        if isinstance(error,PublicationError):
            store.db.execute('INSERT OR REPLACE INTO publication_retries VALUES (?,?,NULL,1)',(operation['id'],previous['attempts'] if previous else 0))
        return
    attempts=(previous['attempts'] if previous else 0)+(0 if initial else 1)
    delay=0 if initial else DELAYS[min(max(attempts-1,0),len(DELAYS)-1)]
    retry_at=(datetime.fromisoformat(now())+timedelta(seconds=delay)).isoformat()
    store.db.execute('''INSERT INTO publication_retries VALUES (?,?,?,0)
        ON CONFLICT(operation) DO UPDATE SET attempts=excluded.attempts,retry_at=excluded.retry_at''',
        (operation['id'],attempts,retry_at))
    if not initial:
        old=store.get('publication_backoff',{})
        streak=old.get('attempts',0)+1
        until=(datetime.fromisoformat(now())+timedelta(seconds=DELAYS[min(streak-1,2)])).isoformat()
        store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('publication_backoff',json.dumps({'attempts':streak,'retry_at':until,'error':str(error)})))


def schedule(root):
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        # Only the newest operation matching the still-approved immutable batch
        # can recover. Declines, replacement manifests and explicit pauses win.
        for row in store.db.execute('''SELECT o.* FROM operations o JOIN batches b
            ON b.id=json_extract(o.payload,'$.batch_id')
            LEFT JOIN publication_retries r ON r.operation=o.id
            WHERE o.kind='publish' AND o.state='interrupted' AND (r.operation IS NULL OR r.retry_at IS NULL AND r.paused=0)
            AND b.state='publication_requested' AND b.manifest_sha256=json_extract(o.payload,'$.manifest_sha256')
            AND o.rowid=(SELECT MAX(n.rowid) FROM operations n WHERE n.kind='publish'
                AND json_extract(n.payload,'$.batch_id')=b.id)''').fetchall():
            register(store,unpack(row),row['error'],initial=True)
        if active_operation(store,Lane.INDEXING):
            store.db.commit();return
        backoff=store.get('publication_backoff',{})
        if backoff.get('retry_at','')>now():
            store.db.commit();return
        row=store.db.execute('''SELECT o.id FROM operations o JOIN publication_retries r ON r.operation=o.id
            JOIN batches b ON b.id=json_extract(o.payload,'$.batch_id')
            WHERE o.kind='publish' AND o.state='interrupted' AND r.paused=0 AND r.retry_at<=?
            AND b.state='publication_requested' AND b.manifest_sha256=json_extract(o.payload,'$.manifest_sha256')
            AND o.rowid=(SELECT MAX(n.rowid) FROM operations n WHERE n.kind='publish'
                AND json_extract(n.payload,'$.batch_id')=b.id)
            ORDER BY r.retry_at,o.updated,o.rowid LIMIT 1''',(now(),)).fetchone()
        if row:
            store.db.execute("UPDATE operations SET state='queued',error=NULL,updated=? WHERE id=?",(now(),row['id']))
            store.db.execute('UPDATE publication_retries SET retry_at=NULL WHERE operation=?',(row['id'],))
        store.db.commit()


def paused(root):
    with connect(root) as store:return store.get('publication_backoff',{}).get('retry_at','')>now()


def detail(store,operation):
    row=store.db.execute('SELECT attempts,retry_at,paused FROM publication_retries WHERE operation=?',(operation,)).fetchone()
    return dict(row) if row else None


def pause(store,identifier):
    row=store.db.execute("SELECT id FROM operations WHERE id=? AND kind='publish' AND state='interrupted'",(identifier,)).fetchone()
    if not row or not store.db.execute('SELECT 1 FROM publication_retries WHERE operation=? AND paused=0 AND retry_at IS NOT NULL',(identifier,)).fetchone():
        raise CrawlError('Publication retry is no longer waiting; refresh before pausing')
    store.db.execute('UPDATE publication_retries SET paused=1 WHERE operation=?',(identifier,))


def summary(store):
    rows=store.db.execute('''SELECT o.id,o.error,o.updated,r.retry_at,r.paused FROM operations o
        JOIN batches b ON b.id=json_extract(o.payload,'$.batch_id')
        LEFT JOIN publication_retries r ON r.operation=o.id
        WHERE o.kind='publish' AND o.state='interrupted' AND b.state='publication_requested'
        AND b.manifest_sha256=json_extract(o.payload,'$.manifest_sha256')
        AND o.rowid=(SELECT MAX(n.rowid) FROM operations n WHERE n.kind='publish'
            AND json_extract(n.payload,'$.batch_id')=b.id) ORDER BY o.updated DESC''').fetchall()
    retries=[r for r in rows if r['retry_at'] and not r['paused']]
    latest=store.db.execute("SELECT MAX(updated) FROM batches WHERE state='indexed'").fetchone()[0]
    return {'waiting':len(rows),'retrying':len(retries),'attention':len(rows)-len(retries),
            'next_retry':min((r['retry_at'] for r in retries),default=None),
            'error':rows[0]['error'] if rows else None,'last_indexed':latest,
            'backoff':store.get('publication_backoff',{})}
