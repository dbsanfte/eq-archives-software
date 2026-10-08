"""Explicit whole-board capture and metadata-only consolidation for SitePowerUp."""
import json
from pathlib import Path

from common import CrawlError, Store, digest, now, site_identity
from sitepowerup import address, board_url
from sitepowerup_capture import Capture, LIMITS

EDITABLE = {'approval_pending', 'discovered', 'sampled', 'sample_error', 'unavailable',
            'identity_unresolved', 'grade_error', 'coverage_unverified'}


def consolidate(root):
    from state import connect
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        rows = [dict(row) for row in store.candidates() if address(row['url'])]
        protected = {row[0] for row in store.db.execute("SELECT json_extract(payload,'$.id') FROM operations WHERE kind='candidate_check' AND state IN ('queued','running')")}
        def preserve(row):
            return (row['state'] not in EDITABLE or row['decision'] or row['id'] in protected
                    or json.loads(row['coverage'] or '{}').get('scope_mode') in ('page', 'custom'))
        owners = {}
        ordered = sorted(rows, key=lambda row: (not preserve(row), row['url'] != board_url(row['url'])))
        for row in ordered:
            if row['state'] == 'duplicate_candidate':
                continue
            if preserve(row) or row['url'] == board_url(row['url']):
                owners.setdefault(site_identity(row['url']), row)
        for row in rows:
            if preserve(row):
                continue
            parent, identity = board_url(row['url']), site_identity(row['url'])
            owner = owners.get(identity)
            if owner and owner['id'] == row['id']:
                coverage = json.loads(row['coverage'] or '{}')
                if coverage.get('scope_mode', 'directory') in ('directory', 'site'):
                    coverage['scope_mode'] = 'sitepowerup'
                    store.db.execute('UPDATE candidates SET scope=?,coverage=? WHERE id=?', (parent, json.dumps(coverage), row['id']))
                continue
            if not owner:
                if address(row['url'])['kind'] == 'form':
                    store.db.execute("UPDATE candidates SET state='rejected',error=? WHERE id=?",
                                     ('Posting or administration form; use the board index for discovery.', row['id']))
                    continue
                owner = {**row, 'id': digest(parent)[:24], 'url': parent, 'scope': parent, 'decision': None}
                coverage = json.loads(owner['coverage'] or '{}')
                coverage.pop('site_check', None)
                coverage.update(scope_mode='sitepowerup', resolved_from=row['url'])
                owner['coverage'] = json.dumps(coverage)
                keys = list(owner)
                store.db.execute(f"INSERT INTO candidates({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", list(owner.values()))
                owners[identity] = owner
            coverage = json.loads(row['coverage'] or '{}')
            coverage.update(duplicate_of=owner['id'], resolved_board_url=parent)
            store.db.execute("UPDATE candidates SET state='duplicate_candidate',coverage=? WHERE id=?", (json.dumps(coverage), row['id']))
            store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                             (row['id'], 'sitepowerup_consolidated', json.dumps({'board_candidate': owner['id'], 'board_url': parent}), now()))
        store.db.commit()


def capture_board(root, batch_id, site, downloader_factory, progress=None):
    from captures import check_manifest
    from indexer.capture_enrichment import DEFAULT_POLICY
    directory = Path(root) / 'batches' / batch_id / 'sitepowerup'
    store = Store(directory)
    try:
        runner = Capture(store)
        if not runner.config:
            runner.plan(site['scope'], limits=LIMITS)
        elif site['scope'] != runner.config['url']:
            raise CrawlError('Approved SitePowerUp board scope changed after capture started')
        runner.seed(site.get('reviewed_captures', site.get('captures', [])), root)
        def report(status):
            if progress:
                progress({'phase': status.get('phase', 'capturing'), 'files': status['counts'].get('captured', 0),
                          'bytes': status['transport'].get('bytes', 0),
                          'urls_checked': sum(status['counts'].values()) - status['counts'].get('pending', 0),
                          'sites_done': 0, 'sites_total': 1, 'site_url': site['url'],
                          'sitepowerup': status, 'current_url': status.get('current_url')})
        result = runner.run(downloader_factory, report)
        if result['coverage']['state'] == 'paused':
            raise CrawlError(result['coverage']['reason'])
        prefix = str(directory.relative_to(root)) + '/'
        captures = [{**capture, 'path': prefix + capture['path'], 'candidate_id': site['id']} for capture in result['captures']]
        if not captures:
            raise CrawlError('No verified SitePowerUp discussion pages were recovered. ' + result['coverage']['reason'])
        manifest = {'schema': 1, 'batch_id': batch_id, 'created_at': result['created_at'], 'sites': [site],
                    'capture_window': result['capture_window'], 'captures': captures, 'limits': LIMITS,
                    'indexing': dict(DEFAULT_POLICY), 'sitepowerup': {'board': result['board'], 'coverage': result['coverage']},
                    'notes': result['notes'] + [{'url': site['url'], 'reason': result['coverage']['reason']}], 'visited': []}
        check_manifest(root, manifest)
        if progress:
            progress({'phase': 'ready_for_review', 'files': len(captures), 'bytes': sum(c['bytes'] for c in captures),
                      'urls_checked': sum(result['coverage']['counts'].values()), 'sites_done': 1, 'sites_total': 1,
                      'site_url': site['url'], 'sitepowerup': result['coverage']})
        return manifest
    finally:
        store.close()
