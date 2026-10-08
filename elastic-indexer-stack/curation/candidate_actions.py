"""Atomic, reversible dismissal of the complete Candidates stage."""
import json

from common import CrawlError, candidate_exclusion, now
from capture_flow import Action, transition
from portal import Stage, decorate, dismissal_preview
from review import queue
from state import connect, identifier


def retire_excluded(root):
    """Retire known promotions atomically, without inventing human decisions."""
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        active = {row[0] for row in store.db.execute("SELECT json_extract(payload,'$.id') FROM operations WHERE kind='candidate_check' AND state IN ('queued','running')")}
        for row in decorate(store, queue(store)):
            reason = candidate_exclusion(row['url'])
            if not reason or row['stage'] != Stage.CANDIDATES or row['decision'] or row['id'] in active:
                continue
            coverage = {**(row['coverage'] or {}), 'candidate_exclusion': reason}
            state = transition(row['state'], Action.REJECT)
            store.db.execute('UPDATE candidates SET state=?,coverage=?,error=? WHERE id=?',
                             (state, json.dumps(coverage), reason, row['id']))
            store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                             (row['id'], 'candidate_excluded', json.dumps({'reason': reason}), now()))
        store.db.commit()


def dismiss_all(store, token):
    store.db.execute('BEGIN IMMEDIATE')
    rows = decorate(store, queue(store))
    preview = dismissal_preview(rows)
    if token != preview['token'] or not preview['count']:
        raise CrawlError('Candidates changed. Review the new count and try Dismiss all again.')
    batch = identifier()
    members = []
    for row in rows:
        if row['stage'] != Stage.CANDIDATES:
            continue
        state = transition(row['state'], Action.REJECT)
        grant = {'id': row['id'], 'manifest_sha256': row['manifest_sha256'], 'decision': 'reject',
                 'reviewed_at': now(), 'dismissal': batch}
        store.db.execute('UPDATE candidates SET state=?,decision=? WHERE id=?', (state, json.dumps(grant), row['id']))
        store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                         (row['id'], 'candidate_dismissed', json.dumps(grant), now()))
        members.append({'id': row['id'], 'manifest_sha256': row['manifest_sha256']})
    store.db.execute('INSERT INTO meta(key,value) VALUES (?,?)', ('dismissal:' + batch, json.dumps(members)))
    store.db.commit()
    return {'dismissed': len(members), 'dismissal': batch}


def undo_dismissal(store, batch):
    store.db.execute('BEGIN IMMEDIATE')
    members = store.get('dismissal:' + batch)
    if not members:
        raise CrawlError('This dismissal is no longer available to undo.')
    current = {row['id']: row for row in decorate(store, queue(store))}
    for member in members:
        row = current.get(member['id'])
        if (not row or row['state'] != 'rejected' or row['manifest_sha256'] != member['manifest_sha256']
                or (row['decision'] or {}).get('dismissal') != batch):
            raise CrawlError('A dismissed site changed. Restore remaining sites individually from History.')
    for member in members:
        state = transition(current[member['id']]['state'], Action.RESTORE)
        store.db.execute('UPDATE candidates SET state=?,decision=NULL WHERE id=?', (state, member['id']))
        store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                         (member['id'], 'candidate_restored', json.dumps({'dismissal': batch}), now()))
    store.db.execute('DELETE FROM meta WHERE key=?', ('dismissal:' + batch,))
    store.db.commit()
    return {'restored': len(members)}
