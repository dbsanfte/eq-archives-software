"""Whole-site review decisions and atomic, snapshot-bound bulk actions."""
import json

from capture_flow import Action, transition
from capture_continuation import require_complete
from captures import check_manifest
from common import CrawlError, digest, now
from coverage_check import require_new
from indexer.capture_enrichment import DEFAULT_POLICY, policy
from portal import decorate
from review import queue
from site_reviews import get
from state import connect, enqueue, identifier


def preview(store, rows=None):
    rows = decorate(store, queue(store)) if rows is None else rows
    batches = {row['id']: dict(row) for row in store.db.execute("""SELECT id,state,manifest_sha256,
        json_array_length(manifest,'$.captures') AS files FROM batches WHERE state='awaiting_review'""")}
    sites = []
    for row in sorted(rows, key=lambda item: item['id']):
        if row.get('review_state') != 'awaiting_review':
            continue
        capture = (row.get('coverage') or {}).get('capture') or {}
        review_id = capture.get('review_id') or capture.get('batch_id')
        batch = batches.get(review_id, {})
        sites.append({'id': row['id'], 'scope': row['scope'], 'state': row['state'],
                      'candidate_hash': row['manifest_sha256'], 'review_id': review_id,
                      'needs_regeneration': capture.get('needs_regeneration', False),
                      'manifest_sha256': batch.get('manifest_sha256'), 'files': batch.get('files', 0)})
    return {'count': len(sites), 'token': digest(sites), 'sites': sites,
            'incomplete_count': sum(site['needs_regeneration'] for site in sites),
            'files': sum(site['files'] or 0 for site in sites),
            'max_enrichment_usd': len(sites) * DEFAULT_POLICY['max_enrichment_usd']}


def validate_decision(store, reviewed, decision):
    expected = 'indexing_declined' if decision == 'reconsider' else 'awaiting_review'
    if reviewed['state'] != expected:
        raise CrawlError('Site decision is no longer available')
    if decision == 'approve':
        require_complete(reviewed['manifest'])
    candidate = reviewed['manifest']['sites'][0]['id']
    row = store.db.execute('SELECT state FROM candidates WHERE id=?', (candidate,)).fetchone()
    if not row:
        raise CrawlError('Captured site candidate is unavailable')
    action = {'approve': Action.APPROVE_INDEX, 'decline': Action.DECLINE_INDEX,
              'reconsider': Action.RECONSIDER_INDEX}[decision]
    return transition(row['state'], action)


def apply_decision(store, reviewed, decision, bulk=None, automatic=False):
    """Caller owns the write transaction and validates approval sources first."""
    state = validate_decision(store, reviewed, decision)
    candidate = reviewed['manifest']['sites'][0]['id']
    operation = None
    if decision == 'approve':
        operation = enqueue(store, 'publish', {'batch_id': reviewed['id'],
                            'manifest_sha256': reviewed['manifest_sha256']}, commit=False)
        batch_state = 'publication_requested'
    else:
        batch_state = 'indexing_declined' if decision == 'decline' else 'awaiting_review'
    store.db.execute('UPDATE batches SET state=?,updated=? WHERE id=?', (batch_state, now(), reviewed['id']))
    store.db.execute('UPDATE candidates SET state=? WHERE id=?', (state, candidate))
    detail = {'id': reviewed['id'], 'manifest_sha256': reviewed['manifest_sha256'], 'decision': decision}
    if automatic:
        detail['authorization'] = 'original_site_capture_approval'
    if bulk:
        detail['bulk'] = bulk
    event = store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                            (candidate, 'site_indexing_automatic' if automatic else 'site_indexing_' + decision, json.dumps(detail), now()))
    return {'state': state, 'operation': operation, 'event': event.lastrowid}


def checked_snapshot(store, token):
    current = preview(store)
    if current['token'] != token or not current['count']:
        raise CrawlError('Review sites changed. Refresh and confirm the new list before trying again.')
    return current


def reviewed_site(store, member):
    reviewed = get(store, member['review_id'])
    if (reviewed['manifest_sha256'] != member['manifest_sha256']
            or reviewed['manifest']['sites'][0]['id'] != member['id']):
        raise CrawlError('Captured site changed since review')
    return reviewed


def decide_all(root, token, decision):
    # Verify files before taking the write lock: large approvals must not hold up
    # worker progress or turn every status poll into a source read.
    with connect(root) as store:
        snapshot = checked_snapshot(store, token)
        for member in snapshot['sites']:
            reviewed = reviewed_site(store, member)
            validate_decision(store, reviewed, decision)
            if decision == 'approve':
                try:
                    require_new(store, reviewed['manifest']['sites'][0]['url'])
                    policy(reviewed['manifest'])
                    check_manifest(root, reviewed['manifest'])
                except CrawlError as error:
                    raise CrawlError(f"{member['scope']}: {error}. No sites were approved.") from None
        store.db.execute('BEGIN IMMEDIATE')
        checked_snapshot(store, token)
        bulk = identifier()
        members = []
        for member in snapshot['sites']:
            result = apply_decision(store, reviewed_site(store, member), decision, bulk)
            members.append({**member, 'event': result['event']})
        if decision == 'decline':
            store.db.execute('INSERT INTO meta(key,value) VALUES (?,?)', ('review-dismissal:' + bulk, json.dumps(members)))
        store.db.commit()
    return {'count': len(members), 'decision': decision, 'dismissal': bulk if decision == 'decline' else None}


def undo_dismissal(root, dismissal):
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        members = store.get('review-dismissal:' + dismissal)
        if not members:
            raise CrawlError('This review dismissal is no longer available to undo.')
        reviewed = []
        for member in members:
            latest = store.db.execute('SELECT MAX(id) FROM events WHERE candidate=?', (member['id'],)).fetchone()[0]
            site = reviewed_site(store, member)
            if latest != member['event'] or site['state'] != 'indexing_declined':
                raise CrawlError('A dismissed site changed. Reconsider remaining sites individually in History.')
            validate_decision(store, site, 'reconsider')
            reviewed.append(site)
        for site in reviewed:
            apply_decision(store, site, 'reconsider', dismissal)
        store.db.execute('DELETE FROM meta WHERE key=?', ('review-dismissal:' + dismissal,))
        store.db.commit()
    return {'restored': len(members)}
