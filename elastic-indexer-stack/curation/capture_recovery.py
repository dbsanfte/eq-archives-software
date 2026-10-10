"""Periodic recovery of approved captures, independent of paid automatic mode."""
from datetime import datetime, timedelta
import json

from capture_failures import temporary, unavailable
from common import CrawlError, now
from state import connect, unpack

DELAYS = (300, 900, 3600)


def register(store, operation, error, *, initial=False):
    if not temporary(error) and not (str(error).startswith('Wayback HTTP ') and unavailable(error)):
        return
    previous = store.db.execute('SELECT * FROM capture_retries WHERE operation=?', (operation['id'],)).fetchone()
    if previous and previous['paused']:
        return
    attempt = (previous['attempts'] if previous else 0) + (0 if initial else 1)
    delay = DELAYS[min(max(attempt - 1, 0), len(DELAYS) - 1)]
    retry_at = (datetime.fromisoformat(now()) + timedelta(seconds=delay)).isoformat()
    store.db.execute('''INSERT INTO capture_retries VALUES (?,?,?,0)
        ON CONFLICT(operation) DO UPDATE SET attempts=excluded.attempts,retry_at=excluded.retry_at''',
        (operation['id'], attempt, retry_at))


def schedule(root):
    from capture_queue import queue_resume
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        for row in store.db.execute("""SELECT o.* FROM operations o
            LEFT JOIN capture_retries r ON r.operation=o.id
            WHERE o.kind='capture' AND o.state='interrupted' AND r.operation IS NULL""").fetchall():
            last = store.db.execute("""SELECT action FROM events WHERE json_extract(detail,'$.operation')=?
                AND action IN ('queue_capture_resume','cancel_capture_resume') ORDER BY rowid DESC LIMIT 1""", (row['id'],)).fetchone()
            if not last or last['action'] != 'cancel_capture_resume':
                register(store, unpack(row), row['error'], initial=True)
        for row in store.db.execute("""SELECT o.* FROM operations o JOIN capture_retries r ON r.operation=o.id
            WHERE o.kind='capture' AND o.state='interrupted' AND r.paused=0 AND r.retry_at<=?
            ORDER BY r.retry_at,o.created,o.rowid""", (now(),)).fetchall():
            try:
                queue_resume(store, unpack(row), automatic=True)
            except CrawlError as error:
                # Changed decisions/scope or a newer capture need human review.
                store.db.execute('UPDATE capture_retries SET paused=1 WHERE operation=?', (row['id'],))
                store.db.execute('UPDATE operations SET error=? WHERE id=?', (str(error), row['id']))
        store.db.commit()


def detail(store, operation):
    row = store.db.execute('SELECT attempts,retry_at,paused FROM capture_retries WHERE operation=?', (operation,)).fetchone()
    return dict(row) if row else None


def pause(store, operation):
    row = store.db.execute("SELECT id FROM operations WHERE id=? AND kind='capture' AND state='interrupted'", (operation,)).fetchone()
    if not row or not store.db.execute('SELECT 1 FROM capture_retries WHERE operation=? AND paused=0 AND retry_at IS NOT NULL', (operation,)).fetchone():
        raise CrawlError('Automatic retry is no longer waiting; refresh before pausing')
    store.db.execute('UPDATE capture_retries SET paused=1 WHERE operation=?', (operation,))
    store.db.execute("INSERT INTO events(candidate,action,detail,created) VALUES (NULL,'capture_retries_paused',?,?)",
                     (json.dumps({'operation': operation}), now()))
