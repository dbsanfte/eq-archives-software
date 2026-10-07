"""Atomically claim bounded batches from an unbounded approval queue."""
from capture_flow import Action, transition
from common import CrawlError, now, original_url, within_scope
from captures import LIMITS
from review import checked_sources, record
from state import enqueue, identifier


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
        checked_sources(store, site)
        snapshots = [item for item in site['captures'] if
                     (original_url(item['url']) == original_url(site['url']) if site['scope_mode'] == 'page'
                      else within_scope(item['url'], site['scope']))]
        if not snapshots and site['scope_mode'] != 'custom':
            raise CrawlError('Approved scope excludes its reviewed source')
        sites.append({**{key: site[key] for key in ('id','url','scope','scope_mode','manifest_sha256','decision')},
                      'captures': snapshots, 'reviewed_captures': site['captures']})
    if not 1 <= len(sites) <= LIMITS['sites']:
        raise CrawlError('Capture batches require 1–5 sites')
    batch_id = identifier()
    operation = enqueue(store, 'capture', {'batch_id': batch_id, 'sites': sites}, commit=False)
    store.db.execute("INSERT INTO batches VALUES (?,'capturing',NULL,NULL,NULL,NULL,NULL,?,?)", (batch_id, now(), now()))
    for site in sites:
        store.db.execute('UPDATE candidates SET state=? WHERE id=?', (transition('approved_waiting_batch', Action.START), site['id']))
    return operation, batch_id
