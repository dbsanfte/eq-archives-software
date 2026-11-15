"""Typed storage diagnostics and bounded metadata retries, without resetting work."""
from datetime import datetime, timedelta
import json
import sqlite3

from capture_failures import STORAGE_BUSY
from common import now
from state import Lane, connect, database_busy


def diagnostic(error):
    if not isinstance(error, sqlite3.Error):
        return None
    code = getattr(error, 'sqlite_errorcode', None)
    base = (code & 255) if type(code) is int else None
    category = ('storage_busy' if database_busy(error) else
                {sqlite3.SQLITE_FULL: 'storage_full', sqlite3.SQLITE_READONLY: 'storage_readonly',
                 sqlite3.SQLITE_IOERR: 'storage_io', sqlite3.SQLITE_CORRUPT: 'storage_corrupt',
                 sqlite3.SQLITE_NOTADB: 'storage_corrupt'}.get(base, 'storage_error'))
    return {'category': category, 'sqlite_code': code,
            'retryable': category == 'storage_busy'}


def message(detail):
    text = {
        'storage_busy': STORAGE_BUSY,
        'storage_full': 'Curation storage is full; free space before retrying. Saved work is retained.',
        'storage_readonly': 'Curation storage is read-only; restore writable storage before retrying.',
        'storage_io': 'Curation storage reported an I/O error; check the volume before retrying.',
        'storage_corrupt': 'Curation database integrity needs attention; restore or repair storage before retrying.',
        'storage_error': 'Curation database operation failed; its diagnostic code is saved for investigation.',
    }[detail['category']]
    code = detail.get('sqlite_code')
    return text + (f' [SQLite code {code}]' if type(code) is int else '')


def register(store, operation, detail):
    if not detail or not detail['retryable'] or operation['kind'] not in ('discover', 'candidate_check'):
        return
    row = store.db.execute('SELECT attempts FROM operation_storage_retries WHERE operation=?', (operation['id'],)).fetchone()
    attempts = (row[0] if row else 0) + 1
    at = (datetime.fromisoformat(now()) + timedelta(seconds=min(300, 5 * 2 ** min(attempts-1, 6)))).isoformat()
    store.db.execute('''INSERT INTO operation_storage_retries VALUES (?,?,?) ON CONFLICT(operation)
        DO UPDATE SET attempts=excluded.attempts,retry_at=excluded.retry_at''', (operation['id'], attempts, at))


def schedule(root, lane):
    if lane != Lane.CANDIDATES:
        return
    with connect(root) as store:
        # Reuse the operation, campaign deadline, evidence and paid ledger. A
        # resumed expired campaign finishes naturally rather than getting an hour.
        row = store.db.execute('''SELECT o.id,o.result FROM operation_storage_retries r
            JOIN operations o ON o.id=r.operation WHERE o.state='interrupted' AND r.retry_at<=?
            AND o.kind IN ('discover','candidate_check')
            AND json_extract(o.result,'$.failure.category')='storage_busy'
            AND json_extract(o.result,'$.failure.retryable')=1
            ORDER BY r.retry_at,o.created LIMIT 1''', (now(),)).fetchone()
        if row:
            store.db.execute('BEGIN IMMEDIATE')
            detail = json.loads(row['result'] or '{}').get('failure') or {}
            if detail.get('category') == 'storage_busy' and detail.get('retryable') is True:
                store.db.execute("UPDATE operations SET state='queued',updated=? WHERE id=? AND state='interrupted' AND result=?",
                                 (now(), row['id'], row['result']))
        store.db.commit()
