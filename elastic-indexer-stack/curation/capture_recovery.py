"""Periodic recovery of approved captures, independent of paid automatic mode."""
from datetime import datetime, timedelta
import json

from capture_failures import temporary, unavailable, LEGACY_RECOVERABLE
from common import CrawlError, now
from state import connect, unpack

DELAYS = (300, 900, 3600)


def register(store, operation, error, *, initial=False):
    legacy = (initial and str(error) in LEGACY_RECOVERABLE and not store.db.execute(
        'SELECT 1 FROM capture_legacy_recovery WHERE operation=?', (operation['id'],)).fetchone())
    if not legacy and not temporary(error) and not (str(error).startswith('Wayback HTTP ') and unavailable(error)):
        return
    previous = store.db.execute('SELECT * FROM capture_retries WHERE operation=?', (operation['id'],)).fetchone()
    if previous and previous['paused']:
        return
    if legacy:
        store.db.execute('INSERT INTO capture_legacy_recovery VALUES (?,?)', (operation['id'], now()))
    attempt = (previous['attempts'] if previous else 0) + (0 if initial else 1)
    delay = DELAYS[0] if legacy else DELAYS[min(max(attempt - 1, 0), len(DELAYS) - 1)]
    retry_at = (datetime.fromisoformat(now()) + timedelta(seconds=delay)).isoformat()
    store.db.execute('''INSERT INTO capture_retries VALUES (?,?,?,0)
        ON CONFLICT(operation) DO UPDATE SET attempts=excluded.attempts,retry_at=excluded.retry_at''',
        (operation['id'], attempt, retry_at))


def schedule(root):
    from capture_queue import queue_resume
    refresh_grading(root)
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        for row in store.db.execute("""SELECT o.* FROM operations o
            LEFT JOIN capture_retries r ON r.operation=o.id
            WHERE o.kind='capture' AND o.state='interrupted'
            AND (r.operation IS NULL OR r.paused=0 AND r.retry_at IS NULL)""").fetchall():
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


def refresh_grading(root):
    """Return one stale automatic approval for grading; never waive its proof."""
    from automation import budget
    from grading import judgment_signature
    from review import StaleJudgment, checked_sources, record
    if not budget(root).settings()['enabled']:
        return
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        raw = store.db.execute("""SELECT c.* FROM capture_queue_failures f JOIN candidates c
            ON c.id=f.candidate AND c.decision=f.decision
            WHERE c.state='approved_waiting_batch'
              AND json_extract(c.decision,'$.origin')='automatic_policy'
              AND f.error='Source judgment is stale; grade and review again'
            ORDER BY f.created,c.rowid LIMIT 1""").fetchone()
        if raw is None:
            return
        row = record(store, raw)
        if row['decision']['manifest_sha256'] != row['manifest_sha256'] or row['coverage'].get('capture', {}).get('continuation'):
            store.db.execute('UPDATE capture_queue_failures SET error=? WHERE candidate=?',
                             ('Approval changed; review this site before capture.', row['id']))
            store.db.commit()
            return
        try:
            checked_sources(store, row)
        except StaleJudgment:
            from grading import sources
            signature = judgment_signature(sources(store, row, 10000000), row['rating'].get('grading_criteria', ''))
            coverage = {**row['coverage'], 'grading_refresh_required': signature}
            store.db.execute("UPDATE candidates SET state='approval_pending',decision=NULL,coverage=?,error=? WHERE id=?",
                             (json.dumps(coverage), 'Source extraction changed; fresh grading is queued before capture.', row['id']))
            store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                             (row['id'], 'stale_approval_returned', json.dumps({'approval': row['decision'],
                              'rating': row['rating'], 'next_signature': signature}), now()))
        except CrawlError as error:
            # Missing/changed bytes are integrity failures, not a grading upgrade.
            store.db.execute('UPDATE capture_queue_failures SET error=? WHERE candidate=?', (str(error), row['id']))
            store.db.commit()
            return
        store.db.execute('DELETE FROM capture_queue_failures WHERE candidate=?', (row['id'],))
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
