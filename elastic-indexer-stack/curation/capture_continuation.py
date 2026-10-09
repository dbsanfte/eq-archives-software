"""Continue an unapproved ordinary-site capture without changing its old review.

Legacy captures inventory the whole scope; complete captures retry only gaps.
Retained manifests and successful sources stay immutable through either path.
"""
import json
from datetime import datetime, timedelta

from capture_flow import Action, UNDO_SECONDS, transition
from captures import check_manifest
from common import CrawlError, digest, now
from coverage_check import require_new
from full_capture import POLICY
from review import record
from state import connect, unpack, valid_id


def incomplete(manifest):
    sites = manifest.get('sites', [])
    return (manifest.get('capture_policy') != POLICY and len(sites) == 1
            and sites[0].get('scope_mode') in ('page', 'directory', 'site', 'custom')
            and ('limits' in manifest or 'capture_coverage' in manifest))


def retryable(manifest):
    sites = manifest.get('sites', [])
    gaps = manifest.get('capture_retry', {})
    return (manifest.get('capture_policy') == POLICY and len(sites) == 1
            and sites[0].get('scope_mode') in ('page', 'directory', 'site', 'custom')
            and sum(gaps.get(key, 0) for key in ('files', 'lookups')) > 0)


def require_complete(manifest):
    if incomplete(manifest):
        raise CrawlError('This legacy capture has not checked the complete file inventory. Use Regenerate full capture before approving indexing.')


def checked_review(store, review_id, expected):
    row = store.db.execute('SELECT * FROM batches WHERE id=?', (valid_id(review_id),)).fetchone()
    if not row:
        raise CrawlError('Unknown captured site')
    reviewed = unpack(row)
    manifest = reviewed['manifest']
    if (not manifest or reviewed['manifest_sha256'] != expected or digest(manifest) != expected
            or reviewed['state'] != 'awaiting_review' or reviewed['publication'] or reviewed['job']
            or not (incomplete(manifest) or retryable(manifest))):
        raise CrawlError('Only an unchanged, unapproved incomplete capture can be continued')
    site = manifest['sites'][0]
    row = store.db.execute('SELECT * FROM candidates WHERE id=?', (site['id'],)).fetchone()
    if not row:
        raise CrawlError('Captured site candidate is unavailable')
    transition(row['state'], Action.CONTINUE)
    current = record(store, row)
    if not current['decision'] or current['decision'].get('decision') != 'approve':
        raise CrawlError('The original capture approval is unavailable')
    capture = current['coverage'].get('capture', {})
    if ((capture.get('review_id') or capture.get('batch_id')) != review_id
            or current['manifest_sha256'] != site['manifest_sha256']):
        raise CrawlError('The approved site scope or evidence changed since capture')
    require_new(store, site['url'])
    if store.db.execute("SELECT 1 FROM operations WHERE kind='publish' AND json_extract(payload,'$.batch_id')=?", (review_id,)).fetchone():
        raise CrawlError('Publication has already been requested for this capture')
    return reviewed, current


def start(root, review_id, expected):
    with connect(root) as store:
        reviewed, _ = checked_review(store, review_id, expected)
        # Hash the bounded retained sources outside the write lock so polling
        # and independent decisions remain responsive during preflight.
        check_manifest(root, reviewed['manifest'])
        store.db.execute('BEGIN IMMEDIATE')
        reviewed, current = checked_review(store, review_id, expected)
        prior = {'review_id': review_id, 'manifest_sha256': expected}
        if retryable(reviewed['manifest']):
            prior['mode'] = 'retry_failed'
        site = reviewed['manifest']['sites'][0]
        store.db.execute("UPDATE batches SET state='capture_continued',updated=? WHERE id=?", (now(), review_id))
        pending = {**prior, 'previous_decision': current['decision']}
        coverage = {**current['coverage'], 'capture': {**current['coverage']['capture'], 'continuation': pending}}
        decision = {**current['decision'], 'capture_after': (datetime.fromisoformat(now()) + timedelta(seconds=UNDO_SECONDS)).isoformat()}
        store.db.execute('UPDATE candidates SET state=?,coverage=?,decision=? WHERE id=?',
                         (transition(current['state'], Action.CONTINUE), json.dumps(coverage), json.dumps(decision), site['id']))
        store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                         (site['id'], Action.CONTINUE.value, json.dumps(prior), now()))
        store.db.commit()
    return {'state': 'approved_waiting_batch', 'candidate': site['id']}


def undo(store, current):
    """Caller holds the same lock used by queue claiming."""
    state = transition(current['state'], Action.UNDO_CONTINUE)
    coverage = current['coverage']
    prior = coverage['capture'].pop('continuation')
    restored = store.db.execute("UPDATE batches SET state='awaiting_review',updated=? WHERE id=? AND state='capture_continued' AND manifest_sha256=?",
                                (now(), prior['review_id'], prior['manifest_sha256']))
    if restored.rowcount != 1:
        raise CrawlError('The previous capture review changed; refresh before undoing regeneration')
    store.db.execute('UPDATE candidates SET state=?,coverage=?,decision=? WHERE id=?',
                     (state, json.dumps(coverage), json.dumps(prior['previous_decision']), current['id']))
    store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                     (current['id'], Action.UNDO_CONTINUE.value, json.dumps(prior), now()))
    return state


def queued_site(store, current):
    prior = current['coverage']['capture']['continuation']
    row = store.db.execute('SELECT * FROM batches WHERE id=?', (valid_id(prior['review_id']),)).fetchone()
    if not row:
        raise CrawlError('Retained capture is unavailable')
    reviewed = unpack(row)
    if (reviewed['state'] != 'capture_continued' or reviewed['manifest_sha256'] != prior['manifest_sha256']
            or digest(reviewed['manifest']) != prior['manifest_sha256']):
        raise CrawlError('Retained capture changed while queued')
    site = reviewed['manifest']['sites'][0]
    if site['id'] != current['id'] or site['manifest_sha256'] != current['manifest_sha256']:
        raise CrawlError('The approved site scope or evidence changed while queued')
    return {**site, 'capture_policy': POLICY,
            'continued_from': {key: prior[key] for key in ('review_id', 'manifest_sha256', 'mode') if key in prior}}


def retained_manifest(root, site):
    """Resolve the immutable, hash-bound predecessor again on worker resume."""
    prior = site['continued_from']
    with connect(root) as store:
        row = store.db.execute('SELECT * FROM batches WHERE id=?', (valid_id(prior['review_id']),)).fetchone()
        if not row:
            raise CrawlError('Retained capture is unavailable')
        reviewed = unpack(row)
    manifest = reviewed['manifest']
    if (reviewed['state'] != 'capture_continued' or not manifest or digest(manifest) != prior['manifest_sha256']
            or reviewed['manifest_sha256'] != prior['manifest_sha256'] or len(manifest['sites']) != 1
            or {**manifest['sites'][0], 'capture_policy': POLICY, 'continued_from': prior} != site):
        raise CrawlError('Retained capture or approved scope changed; continuation paused')
    check_manifest(root, manifest)
    return manifest
