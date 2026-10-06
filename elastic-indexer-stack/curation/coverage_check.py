"""Recheck staged candidates without paid calls, archive writes or page walks."""

import json
import os

from common import CrawlError, capture_scope, now, site_identity
from discovery import Archive
from site_inventory import SiteInventory
from state import connect

EDITABLE = {'approval_pending', 'approved_waiting_batch', 'deferred', 'rejected',
            'discovered', 'sampled', 'sample_error', 'unavailable', 'identity_unresolved',
            'already_archived', 'duplicate_candidate', 'coverage_unverified'}


def refresh(root, candidate_id=None, force=False):
    repository = os.environ.get('ARCHIVE_REPO')
    if not repository:
        return
    with connect(root) as store:
        archive = Archive(repository, store)
        inventory = SiteInventory(archive)
        rows = store.candidates()
        owners = {}
        published = {site_identity(row['url']): row['id'] for row in rows if row['state'] in ('published', 'indexed')}
        for row in sorted(rows, key=lambda row: row['state'] in EDITABLE):
            owners.setdefault(site_identity(row['url']), row['id'])
        for row in rows:
            if row['state'] not in EDITABLE or candidate_id and row['id'] != candidate_id:
                continue
            site_check = inventory.check(row['url'], force=force, timestamps=[capture['timestamp'] for capture in json.loads(row['captures'] or '[]')])
            if site_identity(row['url']) in published:
                site_check = {**site_check, 'status': 'already_archived', 'complete': True,
                              'published_candidate': published[site_identity(row['url'])]}
            coverage = json.loads(row['coverage'] or '{}')
            coverage['site_check'] = site_check
            state = row['state']
            scope = row['scope']
            mode = coverage.get('scope_mode', 'directory')
            decision = row['decision']
            if mode != 'custom':
                scope = capture_scope(row['url'], mode)
                if scope != row['scope'] and state == 'approved_waiting_batch':
                    state, decision = 'approval_pending', None
            if site_check['status'] == 'already_archived':
                state = 'already_archived'
            elif owners[site_identity(row['url'])] != row['id']:
                state = 'duplicate_candidate'
                coverage['duplicate_of'] = owners[site_identity(row['url'])]
            elif site_check['status'] == 'inventory_partial':
                state = 'coverage_unverified'
            elif state == 'coverage_unverified':
                state = coverage.pop('previous_state', 'approval_pending')
            if state != row['state']:
                coverage.setdefault('previous_state', row['state'])
                store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                                 (row['id'], 'site_coverage_changed', json.dumps({'previous_state': row['state'], 'state': state, 'coverage': site_check}), now()))
            if coverage != json.loads(row['coverage'] or '{}') or state != row['state'] or scope != row['scope']:
                store.db.execute('UPDATE candidates SET coverage=?,state=?,scope=?,decision=? WHERE id=?', (json.dumps(coverage), state, scope, decision, row['id']))
        store.db.commit()


def require_new(store, url):
    if any(row['state'] in ('published', 'indexed') and site_identity(row['url']) == site_identity(url) for row in store.candidates()):
        raise CrawlError('Website/account was already published by another batch')
    repository = os.environ.get('ARCHIVE_REPO')
    if not repository:
        return
    result = SiteInventory(Archive(repository, store)).check(url)
    if result['status'] != 'new_site':
        raise CrawlError('Website/account is already archived or its coverage is unverified; refresh the queue')
