"""Whole-board portal capture and source-backed consolidation of old suggestions."""
import json
import os
from pathlib import Path

from common import CrawlError, Store, digest, now, site_identity
from ezboard import board_url, candidate, candidate_url, remember_page, shard, source_page
from ezboard_capture import Capture, DEFAULT_LIMITS
from urllib.parse import urlsplit

PORTAL_LIMITS = {**DEFAULT_LIMITS}
EDITABLE = {'approval_pending', 'discovered', 'sampled', 'sample_error', 'unavailable',
            'identity_unresolved', 'grade_error', 'coverage_unverified'}


def consolidate(root):
    from captures import verified_source
    from state import connect
    with connect(root) as store:
        rows = store.candidates()
        reads = size = 0
        for row in rows:
            if not shard(urlsplit(row['url']).hostname or ''):
                continue
            for capture in json.loads(row['captures'] or '[]'):
                key = 'ezboard_identity_scan:' + digest([capture['url'], capture['sha256']])
                if store.get(key):
                    continue
                if reads >= 66 or size + capture['bytes'] > 16 * 1024 * 1024:
                    break
                try:
                    data = verified_source(root, capture)
                    reads += 1; size += len(data)
                    page, _ = source_page(data, capture['url'], capture.get('content_type') or '')
                    remember_page(store, page, capture)
                    store.set(key, True)
                except CrawlError:
                    continue
        # Source reads happen outside the write lock. Re-read human decisions
        # and active checks atomically before changing any suggestion's stage.
        store.db.execute('BEGIN IMMEDIATE')
        rows = store.candidates()
        protected = {row[0] for row in store.db.execute("SELECT json_extract(payload,'$.id') FROM operations WHERE kind='candidate_check' AND state IN ('queued','running')")}
        owners = {}
        for row in sorted(rows, key=lambda row: row['state'] in EDITABLE):
            if row['state'] == 'duplicate_candidate':
                continue
            resolved = candidate_url(store, row['url'])
            if resolved and board_url(resolved) and (board_url(row['url']) or row['state'] not in EDITABLE):
                owners.setdefault(site_identity(row['url'], store), row)
        for row in rows:
            if row['state'] not in EDITABLE or row['decision'] or row['id'] in protected or not shard(urlsplit(row['url']).hostname or ''):
                continue
            parent = candidate_url(store, row['url'])
            if not parent:
                coverage = json.loads(row['coverage'] or '{}')
                coverage['ezboard_parent_required'] = True
                store.db.execute("UPDATE candidates SET state='deferred',coverage=?,error=? WHERE id=?",
                    (json.dumps(coverage), 'Parent board could not be verified from saved evidence. Submit its top-level b… board URL.', row['id']))
                continue
            owner = owners.get(site_identity(parent))
            if owner and owner['id'] == row['id']:
                # Only unapproved suggestions adopt the new, explicit scope.
                coverage = json.loads(row['coverage'] or '{}')
                if coverage.get('scope_mode', 'directory') in ('directory', 'site'):
                    coverage['scope_mode'] = 'ezboard'
                    store.db.execute('UPDATE candidates SET scope=?,coverage=?,decision=NULL WHERE id=?',
                                     (parent, json.dumps(coverage), row['id']))
                continue
            if not owner:
                owner = dict(row)
                owner.update(id=digest(parent)[:24], url=parent, scope=parent, decision=None)
                coverage = json.loads(owner['coverage'] or '{}')
                coverage.pop('site_check', None)
                coverage.update(scope_mode='ezboard', resolved_from=row['url'])
                owner['coverage'] = json.dumps(coverage)
                keys = list(owner)
                store.db.execute(f"INSERT INTO candidates({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", list(owner.values()))
                owners[site_identity(parent)] = owner
            coverage = json.loads(row['coverage'] or '{}')
            coverage.update(duplicate_of=owner['id'], resolved_board_url=parent)
            store.db.execute("UPDATE candidates SET state='duplicate_candidate',coverage=? WHERE id=?", (json.dumps(coverage), row['id']))
            store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                             (row['id'], 'ezboard_consolidated', json.dumps({'board_candidate': owner['id'], 'board_url': parent}), now()))
        store.db.commit()


def capture_board(root, batch_id, site, downloader_factory, progress=None):
    from captures import check_manifest
    from indexer.capture_enrichment import DEFAULT_POLICY
    directory = Path(root) / 'batches' / batch_id / 'ezboard'
    store = Store(directory)
    try:
        runner = Capture(store)
        if not runner.config:
            runner.plan(site['scope'], limits=PORTAL_LIMITS, archive_repo=os.environ.get('ARCHIVE_REPO'))
        elif site['scope'] != runner.config['url']:
            raise CrawlError('Approved board scope changed after capture started')
        runner.seed(site.get('reviewed_captures', site.get('captures', [])), root)
        def report(status):
            if progress:
                progress({'phase': status.get('phase', 'capturing'), 'files': status['counts'].get('captured', 0),
                          'bytes': status['transport'].get('bytes', 0),
                          'urls_checked': sum(status['counts'].values()) - status['counts'].get('pending', 0),
                          'sites_done': 0, 'sites_total': 1, 'site_url': site['url'],
                          'ezboard': status, 'current_url': status.get('current_url')})
        result = runner.run(downloader_factory, report)
        if result['coverage']['state'] == 'paused':
            raise CrawlError(result['coverage']['reason'])
        prefix = str(directory.relative_to(root)) + '/'
        captures = [{**capture, 'path': prefix + capture['path'], 'candidate_id': site['id']} for capture in result['captures']]
        if not captures:
            raise CrawlError('No verified board discussion pages were recovered. ' + result['coverage']['reason'])
        manifest = {'schema': 1, 'batch_id': batch_id, 'created_at': result['created_at'], 'sites': [site],
                    'capture_window': result['capture_window'],
                    'captures': captures, 'limits': PORTAL_LIMITS, 'indexing': dict(DEFAULT_POLICY),
                    'ezboard': {key: result[key] for key in ('board', 'coverage', 'hosts', 'forums')},
                    'notes': result['notes'] + [{'url': site['url'], 'reason': result['coverage']['reason']}], 'visited': []}
        check_manifest(root, manifest)
        if progress:
            progress({'phase': 'ready_for_review', 'files': len(captures), 'bytes': sum(c['bytes'] for c in captures),
                      'urls_checked': sum(result['coverage']['counts'].values()), 'sites_done': 1, 'sites_total': 1,
                      'site_url': site['url'], 'ezboard': result['coverage']})
        return manifest
    finally:
        store.close()
