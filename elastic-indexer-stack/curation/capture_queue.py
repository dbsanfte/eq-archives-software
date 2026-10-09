"""Atomically claim bounded batches from an unbounded approval queue."""
from capture_flow import Action, transition
from common import CrawlError, now, original_url, within_capture_scope
from captures import LIMITS
from full_capture import POLICY
from indexer.capture_enrichment import policy_for_sites
from review import checked_sources, record
from state import enqueue, identifier


def held(store):
    return {row['candidate']: row['error'] for row in store.db.execute("""SELECT f.candidate,f.error
        FROM capture_queue_failures f JOIN candidates c ON c.id=f.candidate
        WHERE c.state='approved_waiting_batch' AND c.decision=f.decision""")}


def attention(store):
    return {'preflight': len(held(store)), 'interrupted': store.db.execute(
        "SELECT COUNT(*) FROM operations WHERE kind='capture' AND state='interrupted'").fetchone()[0]}


def queue_order(store):
    """Use the worker's eligibility time and insertion-order tie break."""
    return [row['id'] for row in store.db.execute("""SELECT c.id FROM candidates c
        LEFT JOIN capture_queue_failures f ON f.candidate=c.id AND f.decision=c.decision
        WHERE c.state='approved_waiting_batch'
        ORDER BY (f.candidate IS NOT NULL),
            COALESCE(json_extract(c.decision,'$.capture_after'), json_extract(c.decision,'$.reviewed_at')),c.rowid""")]


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
