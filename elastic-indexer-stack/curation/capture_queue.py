"""Atomically claim bounded batches from an unbounded approval queue."""
import json

from capture_flow import Action, transition
from common import CrawlError, now, original_url, within_capture_scope
from captures import LIMITS
from full_capture import POLICY
from indexer.capture_enrichment import policy_for_sites
from review import checked_sources, record
from state import enqueue, identifier, unpack


def held(store):
    return {row['candidate']: row['error'] for row in store.db.execute("""SELECT f.candidate,f.error
        FROM capture_queue_failures f JOIN candidates c ON c.id=f.candidate
        WHERE c.state='approved_waiting_batch' AND c.decision=f.decision""")}


def attention(store):
    return {'preflight': len(held(store)), 'interrupted': store.db.execute(
        "SELECT COUNT(*) FROM operations WHERE kind='capture' AND state='interrupted'").fetchone()[0]}


QUEUE = """SELECT c.id,c.decision,c.rowid ordinal,
        COALESCE(json_extract(c.decision,'$.capture_after'),json_extract(c.decision,'$.reviewed_at')) ready,
        (f.candidate IS NOT NULL) held,NULL operation FROM candidates c
        LEFT JOIN capture_queue_failures f ON f.candidate=c.id AND f.decision=c.decision
        WHERE c.state='approved_waiting_batch'
        UNION ALL SELECT c.id,c.decision,c.rowid,o.updated,0,o.id
        FROM operations o,json_each(o.payload,'$.sites') site
        JOIN candidates c ON c.id=json_extract(site.value,'$.id')
        WHERE o.kind='capture' AND o.state='resume_queued' AND c.state='capture_resume_queued'"""


def queue_order(store):
    """Fresh approvals and explicit resumes share one uncapped FIFO."""
    return [row['id'] for row in store.db.execute(f'SELECT * FROM ({QUEUE}) ORDER BY held,ready,ordinal')]


def next_entry(store, at):
    return store.db.execute(f'SELECT * FROM ({QUEUE}) WHERE held=0 AND ready<=? ORDER BY ready,ordinal LIMIT 1', (at,)).fetchone()


def resume_members(store, operation, action):
    sites = operation['payload'].get('sites', [])
    if not sites or len({site['id'] for site in sites}) != len(sites):
        raise CrawlError('Capture resume requires its original sites')
    members = []
    for site in sites:
        row = store.db.execute('SELECT * FROM candidates WHERE id=?', (site['id'],)).fetchone()
        latest = store.db.execute("""SELECT id FROM operations WHERE kind='capture' AND EXISTS
            (SELECT 1 FROM json_each(payload,'$.sites') WHERE json_extract(value,'$.id')=?)
            ORDER BY created DESC,rowid DESC LIMIT 1""", (site['id'],)).fetchone()
        if not row or not latest or latest['id'] != operation['id']:
            raise CrawlError('Capture changed since this operation; refresh before resuming')
        members.append((site['id'], transition(row['state'], action)))
    return members


def move_resume(store, operation, action, state):
    """Caller owns BEGIN IMMEDIATE, shared by explicit resume, cancel and claim."""
    members = resume_members(store, operation, action)
    store.db.execute('UPDATE operations SET state=?,updated=? WHERE id=?', (state, now(), operation['id']))
    for candidate, target in members:
        store.db.execute('UPDATE candidates SET state=? WHERE id=?', (target, candidate))
        store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                         (candidate, action.value, json.dumps({'operation': operation['id']}), now()))
    # Leave the original payload, result/checkpoint and error intact. The worker
    # clears the error only when it starts; cancellation restores the same pause.


def queue_resume(store, operation):
    move_resume(store, operation, Action.QUEUE_RESUME, 'resume_queued')


def cancel_resume(store, operation):
    move_resume(store, operation, Action.CANCEL_RESUME, 'interrupted')


def claim_resume(store, identifier):
    operation = unpack(store.db.execute("SELECT * FROM operations WHERE id=? AND state='resume_queued'", (identifier,)).fetchone())
    try:
        move_resume(store, operation, Action.RESUME, 'queued')
    except CrawlError as error:
        # A stale entry must never monopolize the worker's next turn.
        store.db.execute("UPDATE operations SET state='interrupted',error=?,updated=? WHERE id=?", (str(error), now(), identifier))
        for site in operation['payload'].get('sites', []):
            store.db.execute("""UPDATE candidates SET state='capturing' WHERE id=? AND state='capture_resume_queued'
                AND ?=(SELECT id FROM operations WHERE kind='capture' AND EXISTS
                    (SELECT 1 FROM json_each(payload,'$.sites') WHERE json_extract(value,'$.id')=candidates.id)
                    ORDER BY created DESC,rowid DESC LIMIT 1)""", (site['id'], identifier))


def claim(store, ids):
    """Caller owns BEGIN IMMEDIATE; undo and claiming cannot both win."""
    sites = []
    for candidate in ids:
        row = store.db.execute('SELECT * FROM candidates WHERE id=?', (candidate,)).fetchone()
        if not row:
            raise CrawlError('Capture requires a current site approval')
        transition(row['state'], Action.START)
        site = record(store, row)
        if not site['decision'] or site['decision']['manifest_sha256'] != site['manifest_sha256']:
            raise CrawlError('Site approval is stale')
        if site['coverage'].get('capture', {}).get('continuation'):
            from capture_continuation import queued_site
            sites.append(queued_site(store, site))
            continue
        checked_sources(store, site)
        snapshots = [item for item in site['captures'] if site['scope_mode'] in ('ezboard', 'sitepowerup') or
                     (original_url(item['url']) == original_url(site['url']) if site['scope_mode'] == 'page'
                      else within_capture_scope(item['url'], site['scope']))]
        if not snapshots and site['scope_mode'] != 'custom':
            raise CrawlError('Approved scope excludes its reviewed source')
        sites.append({**{key: site[key] for key in ('id','url','scope','scope_mode','manifest_sha256','decision')},
                      'captures': snapshots, 'reviewed_captures': site['captures'], 'capture_policy': POLICY})
    if not 1 <= len(sites) <= LIMITS['sites']:
        raise CrawlError('Capture batches require 1–5 sites')
    policy_for_sites(sites)  # Reject mixed approvals before claiming/downloading.
    if len(sites) != 1 and any(site.get('continued_from') for site in sites):
        raise CrawlError('Regenerate each site independently; the automatic queue starts one site at a time')
    if len(sites) != 1 and any(site['scope_mode'] in ('ezboard', 'sitepowerup') for site in sites):
        raise CrawlError('Capture each whole board separately from other sites')
    batch_id = identifier()
    automatic = all(site.get('decision', {}).get('origin') == 'automatic_policy' for site in sites)
    operation = enqueue(store, 'capture', {'batch_id': batch_id, 'sites': sites, **({'automatic': True} if automatic else {})}, commit=False)
    store.db.execute("INSERT INTO batches VALUES (?,'capturing',NULL,NULL,NULL,NULL,NULL,?,?)", (batch_id, now(), now()))
    for site in sites:
        store.db.execute('DELETE FROM capture_queue_failures WHERE candidate=?', (site['id'],))
        store.db.execute('UPDATE candidates SET state=? WHERE id=?', (transition('approved_waiting_batch', Action.START), site['id']))
    return operation, batch_id
