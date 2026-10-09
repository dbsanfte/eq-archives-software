"""Continue an ordinary-site capture while retaining earlier manifests and Jobs.

Legacy captures inventory the whole scope; complete captures retry only gaps.
Retained manifests and successful sources stay immutable through either path.
"""
import json
from datetime import datetime, timedelta
import re

from capture_flow import Action, UNDO_SECONDS, transition
from capture_budget import budget_identity, published_manifest
from captures import check_manifest
from common import CrawlError, digest, now
from coverage_check import require_new
from full_capture import POLICY
from review import record
from state import connect, unpack, valid_id

PUBLISHED_STATES = {'published_waiting_index', 'indexing', 'index_failed', 'indexed'}
UNPUBLISHED_STATES = {'awaiting_review', 'index_preflight_failed'}


def is_published(batch):
    return (batch['state'] in PUBLISHED_STATES and
            re.fullmatch(r'[a-f0-9]{40}', (batch.get('publication') or {}).get('commit', '')) is not None)


def require_eligible(store, site, force=False):
    """Only an explicitly queued retry may revisit its own published scope."""
    prior = site.get('continued_from') or {}
    if not prior.get('published'):
        return require_new(store, site['url'], force=force)
    row = store.db.execute('SELECT * FROM batches WHERE id=?', (valid_id(prior['review_id']),)).fetchone()
    if not row:
        raise CrawlError('Published predecessor is unavailable')
    batch = unpack(row)
    manifest = batch['manifest']
    if (not is_published(batch) or not manifest or len(manifest['sites']) != 1
            or batch['manifest_sha256'] != prior['manifest_sha256'] or digest(manifest) != prior['manifest_sha256']
            or {**manifest['sites'][0], 'capture_policy': POLICY, 'continued_from': prior} != site
            or prior.get('mode') != 'retry_failed' or not retryable(manifest)):
        raise CrawlError('Published retry does not match its original site, scope and manifest')
    saved = published_manifest(store.root, prior)
    if saved['publication']['commit'] != batch['publication']['commit']:
        raise CrawlError('Published retry commit changed')


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
        raise CrawlError('This legacy capture has not checked the complete file inventory. Use Regenerate full capture to finish acquisition before automatic indexing.')


def checked_review(store, review_id, expected):
    row = store.db.execute('SELECT * FROM batches WHERE id=?', (valid_id(review_id),)).fetchone()
    if not row:
        raise CrawlError('Unknown captured site')
    reviewed = unpack(row)
    manifest = reviewed['manifest']
    published = is_published(reviewed)
    if (not manifest or reviewed['manifest_sha256'] != expected or digest(manifest) != expected
            or not (published or reviewed['state'] in UNPUBLISHED_STATES and not reviewed['publication'] and not reviewed['job'])
            or not (incomplete(manifest) or retryable(manifest))):
        raise CrawlError('Only an unchanged incomplete capture or saved failed files can be retried')
    site = manifest['sites'][0]
    row = store.db.execute('SELECT * FROM candidates WHERE id=?', (site['id'],)).fetchone()
    if not row:
        raise CrawlError('Captured site candidate is unavailable')
    transition(row['state'], Action.RETRY_PUBLISHED_CAPTURE if published else Action.CONTINUE)
    current = record(store, row)
    if not current['decision'] or current['decision'].get('decision') != 'approve':
        raise CrawlError('The original capture approval is unavailable')
    capture = current['coverage'].get('capture', {})
    if ((capture.get('review_id') or capture.get('batch_id')) != review_id
            or current['manifest_sha256'] != site['manifest_sha256']):
        raise CrawlError('The approved site scope or evidence changed since capture')
    if published and not retryable(manifest):
        raise CrawlError('Published captures can retry only their saved missing files')
    if published:
        saved = published_manifest(store.root, {'review_id': review_id, 'manifest_sha256': expected})
        if saved['publication']['commit'] != reviewed['publication']['commit']:
            raise CrawlError('Published retry commit changed')
        budget_identity(store.root, manifest)
    elif manifest.get('enrichment_budget_id'):
        raise CrawlError('Finish publication of this retry before retrying any remaining missing files')
    if not published:
        require_new(store, site['url'])
    if not published and store.db.execute("SELECT 1 FROM operations WHERE kind='publish' AND json_extract(payload,'$.batch_id')=?", (review_id,)).fetchone():
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
        published = is_published(reviewed)
        if published:
            prior['published'] = True
        site = reviewed['manifest']['sites'][0]
        if not published:
            store.db.execute("UPDATE batches SET state='capture_continued',updated=? WHERE id=?", (now(), review_id))
        pending = {**prior, 'previous_decision': current['decision'], 'previous_state': reviewed['state'], 'previous_error': reviewed['error']}
        coverage = {**current['coverage'], 'capture': {**current['coverage']['capture'], 'continuation': pending}}
        decision = {**current['decision'], 'capture_after': (datetime.fromisoformat(now()) + timedelta(seconds=UNDO_SECONDS)).isoformat()}
        store.db.execute('UPDATE candidates SET state=?,coverage=?,decision=? WHERE id=?',
                         (transition(current['state'], Action.RETRY_PUBLISHED_CAPTURE if published else Action.CONTINUE), json.dumps(coverage), json.dumps(decision), site['id']))
        store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                         (site['id'], Action.CONTINUE.value, json.dumps(prior), now()))
        store.db.commit()
    return {'state': 'approved_waiting_batch', 'candidate': site['id']}


def undo(store, current):
    """Caller holds the same lock used by queue claiming."""
    coverage = current['coverage']
    prior = coverage['capture'].pop('continuation')
    if prior.get('published'):
        previous = store.db.execute('SELECT * FROM batches WHERE id=? AND manifest_sha256=?', (prior['review_id'], prior['manifest_sha256'])).fetchone()
        if not previous or not is_published(unpack(previous)):
            raise CrawlError('The published capture changed; refresh before undoing retry')
        state = transition(current['state'], Action.UNDO_INDEXED_CAPTURE if previous['state'] == 'indexed' else Action.UNDO_PUBLISHED_CAPTURE)
    else:
        state = transition(current['state'], Action.UNDO_CONTINUE)
        restored = store.db.execute("UPDATE batches SET state=?,error=?,updated=? WHERE id=? AND state='capture_continued' AND manifest_sha256=?",
                                    (prior.get('previous_state', 'awaiting_review'), prior.get('previous_error'), now(), prior['review_id'], prior['manifest_sha256']))
        if restored.rowcount != 1:
            raise CrawlError('The previous capture changed; refresh before undoing regeneration')
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
    valid_state = is_published(reviewed) if prior.get('published') else reviewed['state'] == 'capture_continued'
    if (not valid_state or reviewed['manifest_sha256'] != prior['manifest_sha256']
            or digest(reviewed['manifest']) != prior['manifest_sha256']):
        raise CrawlError('Retained capture changed while queued')
    site = reviewed['manifest']['sites'][0]
    if site['id'] != current['id'] or site['manifest_sha256'] != current['manifest_sha256']:
        raise CrawlError('The approved site scope or evidence changed while queued')
    return {**site, 'capture_policy': POLICY,
            'continued_from': {key: prior[key] for key in ('review_id', 'manifest_sha256', 'mode', 'published') if key in prior}}


def retained_manifest(root, site):
    """Resolve the immutable, hash-bound predecessor again on worker resume."""
    prior = site['continued_from']
    with connect(root) as store:
        row = store.db.execute('SELECT * FROM batches WHERE id=?', (valid_id(prior['review_id']),)).fetchone()
        if not row:
            raise CrawlError('Retained capture is unavailable')
        reviewed = unpack(row)
    manifest = reviewed['manifest']
    valid_state = is_published(reviewed) if prior.get('published') else reviewed['state'] == 'capture_continued'
    if (not valid_state or not manifest or digest(manifest) != prior['manifest_sha256']
            or reviewed['manifest_sha256'] != prior['manifest_sha256'] or len(manifest['sites']) != 1
            or {**manifest['sites'][0], 'capture_policy': POLICY, 'continued_from': prior} != site):
        raise CrawlError('Retained capture or approved scope changed; continuation paused')
    check_manifest(root, manifest)
    return manifest
