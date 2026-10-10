"""Opt-in continuous curation, with durable policy and explicit human overrides."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

from candidate_checks import require_finished, start as start_check
from common import CrawlError, capture_scope, digest, now
from coverage_check import refresh, require_new
from daily_budget import DailyBudget, DailyBudgetPause, AutomaticStopped
from portal import Stage, candidate_filter, decorate
from review import _apply_decisions, record
from spending import Spending
from state import Lane, OPERATION_KINDS, active_operation, connect, enqueue, unpack
from capture_flow import UNDO_SECONDS

HUMAN_EVENTS = ('approval_undone', 'candidate_restored', 'scope_changed', 'automatic_held')


def budget(root):
    return DailyBudget(Path(root) / 'luna-budget')


def hold(store, identifier, reason):
    # Caller owns the same transaction as Undo/Restore. There is no gap in which
    # the scheduler can approve the row again before the override is recorded.
    store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                     (identifier, 'automatic_held', json.dumps({'reason': reason}), now()))


def eligible(store, row):
    return (row['stage'] == Stage.CANDIDATES and not row['decision']
            and not store.db.execute("SELECT 1 FROM operations WHERE kind='candidate_check' AND state IN ('queued','running') AND json_extract(payload,'$.id')=?", (row['id'],)).fetchone()
            and not store.db.execute(f'''SELECT 1 FROM events WHERE candidate=?
                AND action IN ({','.join('?' for _ in HUMAN_EVENTS)}) LIMIT 1''',
                (row['id'], *HUMAN_EVENTS)).fetchone())


def seed_spend(root, account):
    """Bounded shallow ledger reads; no archive/source enumeration or paid work."""
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    deadline = time.monotonic() + 5
    try:
        for path in Spending(root).ledgers():
            if time.monotonic() > deadline:
                raise CrawlError('Daily accounting is still being initialized. Save settings again to continue safely.')
            db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)
            try:
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                rows = db.execute('''SELECT id,reserved,actual,created FROM attempts
                    WHERE datetime(created)>=datetime(?)''', (day.isoformat(),))
                for identifier, reserved, actual, created in rows:
                    receipt = db.execute('SELECT value FROM meta WHERE key=?', ('daily_reservation:' + str(identifier),)).fetchone()
                    account.import_legacy(digest([str(path.relative_to(root)), identifier]), reserved, actual, created,
                                          json.loads(receipt[0]) if receipt else None)
                    if time.monotonic() > deadline:
                        raise CrawlError('Daily accounting is still being initialized. Save settings again to continue safely.')
            finally:
                db.close()
    except (sqlite3.Error, OSError, ValueError) as error:
        raise CrawlError('Daily Luna accounting is incomplete; automatic mode was not enabled. Retry after staging is available.') from error


def configure(root, payload, kube=None):
    payload = DailyBudget.validate(payload)
    account = budget(root)
    # New Jobs always share this ledger. Existing Jobs must finish untouched;
    # older images cannot be claimed to honor a newly configured daily ceiling.
    if kube and (payload['enabled'] or not account.settings()['configured']):
        from jobs import finished
        for job in kube.jobs():
            if (job['metadata'].get('labels', {}).get('app') == 'eqarchives-capture-import'
                    and not finished(job) and not job['spec'].get('suspend', False)):
                env = job['spec']['template']['spec']['containers'][0].get('env', [])
                if not any(item.get('name') == 'LUNA_DAILY_BUDGET_ROOT' for item in env):
                    raise CrawlError('An earlier enrichment Job is still running without daily budget control. Let it finish before enabling automatic mode.')
    seed_spend(Path(root), account)
    saved = account.configure(payload)
    with connect(root) as store:
        store.event(None, 'automatic_settings', saved)
    return status(root, account)


def status(root, account=None):
    result = (account or budget(root)).snapshot()
    with connect(root) as store:
        result['activity'] = store.get('automatic_activity', {})
        result['held'] = store.db.execute(f"SELECT COUNT(DISTINCT candidate) FROM events WHERE action IN ({','.join('?' for _ in HUMAN_EVENTS)})", HUMAN_EVENTS).fetchone()[0]
    return result


def promote(root, account):
    config = account.settings()
    if not config['enabled']:
        return
    refresh(root)
    with connect(root) as store, account.database() as policy_lock:
        # Source verification is at most one sampled site per tick. Read actual
        # stage metadata, not the paginated list a browser happens to display.
        rows = candidate_filter(decorate(store, [record(store, row) for row in store.db.execute('''SELECT * FROM candidates
            WHERE state='approval_pending' AND rating IS NOT NULL ORDER BY
            json_extract(rating,'$.grade') DESC,priority DESC,rowid''')]))
        row = next((row for row in rows if eligible(store, row) and
                    row['rating'].get('grading_criteria', '') == config['grading_criteria']), None)
        if not row:
            return
        try:
            store.db.execute('BEGIN IMMEDIATE')
            policy_lock.execute('BEGIN IMMEDIATE')
            # This policy revision must still be enabled after acquiring the
            # candidate write lock; current scope/evidence are checked below.
            if json.loads(policy_lock.execute('SELECT value FROM settings WHERE id=1').fetchone()[0]) != config:
                return
            row = decorate(store, [record(store, store.db.execute('SELECT * FROM candidates WHERE id=?', (row['id'],)).fetchone())])[0]
            if not eligible(store, row):
                return
            require_finished(store, row)
            accepted = row['rating']['grade'] >= config['min_grade']
            if accepted:
                require_new(store, row['url'])
                # Pristine directory suggestions become whole sites/accounts.
                # Explicit human scope edits are excluded by eligible().
                if row['scope_mode'] == 'directory':
                    coverage = {**row['coverage'], 'scope_mode': 'site'}
                    scope = capture_scope(row['url'], 'site')
                    store.db.execute('UPDATE candidates SET scope=?,coverage=? WHERE id=?',
                                     (scope, json.dumps(coverage), row['id']))
                    row = record(store, store.db.execute('SELECT * FROM candidates WHERE id=?', (row['id'],)).fetchone())
            choice = {'id': row['id'], 'manifest_sha256': row['manifest_sha256'], 'decision': 'approve' if accepted else 'defer'}
            _apply_decisions(store, [choice], UNDO_SECONDS, require_finished)
            grant = json.loads(store.db.execute('SELECT decision FROM candidates WHERE id=?', (row['id'],)).fetchone()[0])
            grant['automatic'] = {'revision': config['revision'], 'min_grade': config['min_grade'],
                                  'daily_usd': config['daily_usd'], 'grading_criteria': config['grading_criteria'],
                                  'enrichment_budget': 'daily-v1', 'grade': row['rating']['grade']}
            grant['origin'] = 'automatic_policy'
            store.db.execute('UPDATE candidates SET decision=? WHERE id=?', (json.dumps(grant), row['id']))
            store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                             (row['id'], 'automatic_promoted' if accepted else 'automatic_saved', json.dumps(grant), now()))
            store.db.commit()
            return choice
        except Exception as error:
            store.db.rollback()
            # An invalid/stale site cannot repeatedly block every other site.
            detail = str(error) if isinstance(error, CrawlError) else 'Automatic approval failed for this site; saved evidence is retained.'
            hold(store, row['id'], detail)
            store.db.execute('UPDATE candidates SET error=? WHERE id=?', (detail, row['id']))
            store.db.commit()


def schedule(root, account):
    snapshot = account.snapshot()
    config = snapshot['settings']
    if not config['enabled']:
        return
    with connect(root) as store:
        if active := active_operation(store, Lane.CANDIDATES):
            if json.loads(active['payload']).get('automatic'):
                store.set('automatic_activity', {'phase': 'discovering', 'operation': active['id'], 'revision': config['revision']})
            return
        activity = store.get('automatic_activity', {})
        if snapshot['remaining_usd'] <= 0:
            store.set('automatic_activity', {'phase': 'daily_budget', 'retry_at': snapshot['resets_at']})
            return
        # Only automation-owned pauses are resumed. Manual paused runs retain
        # their explicit-resume semantics and their original limits.
        previous = store.db.execute("""SELECT * FROM operations WHERE kind IN ('discover','candidate_check')
            AND json_extract(payload,'$.automatic')=1 ORDER BY created DESC,rowid DESC LIMIT 1""").fetchone()
        previous = unpack(previous) if previous else None
        if previous and previous['state'] == 'interrupted':
            pause = (previous['result'] or {}).get('automatic_pause') or {}
            if pause.get('reason') == 'daily_budget' and snapshot['remaining_usd'] < pause.get('required_usd', 0):
                store.set('automatic_activity', {'phase': 'daily_budget', 'retry_at': snapshot['resets_at'], 'required_usd': pause['required_usd']})
                return
            # A site failure waits for an explicit decision. Its saved operation
            # is not a lane lock; continue checking other sites/discovering.
        # Exhausted link frontiers sleep, instead of creating empty campaigns
        # every ten seconds or resampling remembered low-scoring websites.
        if (activity.get('phase') in ('links_exhausted', 'archive_unavailable') and activity.get('retry_at', '') > now()
                and activity.get('revision') == config['revision']):
            return
        rows = decorate(store, [record(store, row) for row in store.db.execute("""SELECT * FROM candidates
            WHERE state IN ('discovered','sampled','approval_pending') AND rating IS NULL ORDER BY priority DESC,rowid""")])
        for row in rows:
            if not eligible(store, row):
                continue
            if store.db.execute("SELECT 1 FROM operations WHERE kind='candidate_check' AND json_extract(payload,'$.id')=?", (row['id'],)).fetchone():
                continue
            try:
                start_check(store, {'id': row['id'], 'manifest_sha256': row['manifest_sha256'],
                                   'max_usd': 2, 'grading_criteria': config['grading_criteria'], 'automatic': True})
            except Exception as error:
                store.db.rollback()
                detail = str(error) if isinstance(error, CrawlError) else 'Evidence check could not start for this site; saved evidence is retained.'
                hold(store, row['id'], detail)
                store.db.execute('UPDATE candidates SET error=? WHERE id=?', (detail, row['id']))
                store.db.commit()
            return
        operation = enqueue(store, 'discover', {'automatic': True, 'fill_queue': True, 'max_candidates': 50,
            'max_usd': 2, 'min_grade': config['min_grade'], 'grading_criteria': config['grading_criteria']})
        store.set('automatic_activity', {'phase': 'discovering', 'operation': operation, 'revision': config['revision']})


def finished(root, operation, result):
    if not operation['payload'].get('automatic') or operation['kind'] != 'discover':
        return
    reason = result.get('progress', {}).get('stop_reason')
    with connect(root) as store:
        config = budget(root).settings()
        store.set('automatic_activity', {'phase': reason or 'ready', 'operation': operation['id'],
                  'revision': config['revision'],
                  'retry_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                      if reason in ('links_exhausted', 'archive_unavailable') else now()})


def pause_detail(error, operation=None):
    if isinstance(error, DailyBudgetPause):
        return {'reason': 'daily_budget', 'required_usd': error.amount, 'retry_at': error.retry_at}
    if isinstance(error, AutomaticStopped):
        return {'reason': 'disabled'}
    return None


def resume_owned(root, lane, account):
    snapshot = account.snapshot()
    if not snapshot['settings']['enabled']:
        return
    kinds = OPERATION_KINDS[Lane(lane)]
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        if active_operation(store, lane):
            return
        rows = store.db.execute(f"""SELECT * FROM operations WHERE state='interrupted'
            AND json_extract(payload,'$.automatic')=1 AND kind IN ({','.join('?' for _ in kinds)})
            ORDER BY updated,created,rowid""", kinds).fetchall()
        for row in rows:
            operation = unpack(row)
            last_resume = store.db.execute("""SELECT action FROM events WHERE json_extract(detail,'$.operation')=?
                AND action IN ('queue_capture_resume','cancel_capture_resume') ORDER BY rowid DESC LIMIT 1""", (operation['id'],)).fetchone()
            if last_resume and last_resume['action'] == 'cancel_capture_resume':
                continue  # A human cancelled this resume, including an owned restart.
            pause = (operation['result'] or {}).get('automatic_pause') or {}
            reason = pause.get('reason')
            allowed = (reason == 'disabled' or
                       reason == 'daily_budget' and snapshot['remaining_usd'] >= pause.get('required_usd', float('inf')) or
                       operation['error'] == 'Worker stopped; resume explicitly')
            if allowed:
                store.db.execute("UPDATE operations SET state='queued',error=NULL,updated=? WHERE id=?", (now(), operation['id']))
                store.db.commit()
                return
