"""Publish completed captures under their original site approval, once."""
import json

from capture_continuation import require_complete, require_eligible
from capture_budget import budget_identity
from captures import check_manifest
from common import CrawlError, now
from indexer.capture_enrichment import policy
from review import record
from review_actions import apply_decision
from site_reviews import get
from state import connect, valid_id


def approved_site(store, captured):
    manifest = captured['manifest']
    if captured['state'] != 'awaiting_review' or len(manifest['sites']) != 1:
        raise CrawlError('This captured site is no longer awaiting automatic indexing')
    site = manifest['sites'][0]
    raw = store.db.execute('SELECT * FROM candidates WHERE id=?', (site['id'],)).fetchone()
    if not raw:
        raise CrawlError('The original capture approval is unavailable')
    candidate = record(store, raw)
    approval = candidate['decision'] or {}
    capture = candidate['coverage'].get('capture', {})
    if (candidate['state'] != 'captured_awaiting_review' or approval.get('decision') != 'approve'
            or candidate['manifest_sha256'] != site['manifest_sha256']
            or approval.get('manifest_sha256') != site['manifest_sha256']
            or (capture.get('review_id') or capture.get('batch_id')) != captured['id']):
        raise CrawlError('The original site capture approval no longer matches. Automatic indexing is paused.')
    require_complete(manifest)
    policy(manifest)
    return site, approval


def prepare_next(root):
    """Preflight one complete site outside the write lock; failures need retry."""
    with connect(root) as store:
        pending = store.db.execute("SELECT id,manifest_sha256 FROM batches WHERE state='awaiting_review' ORDER BY created,rowid LIMIT 1").fetchone()
        if not pending:
            return
        identifier, expected = pending['id'], pending['manifest_sha256']
        try:
            captured = get(store, identifier)
            site, approval = approved_site(store, captured)
            # An explicit preparation retry resumes bounded partial coverage
            # checks; returning their cached pause forever cannot unblock it.
            require_eligible(store, site, force=True)
            budget_identity(root, captured['manifest'])
            check_manifest(root, captured['manifest'])
            # A decline, regeneration or changed manifest during source checks
            # cannot be replaced by an automatic publication request.
            store.db.execute('BEGIN IMMEDIATE')
            current = get(store, identifier)
            _, current_approval = approved_site(store, current)
            if current['manifest_sha256'] != expected or current_approval != approval:
                raise CrawlError('The captured site changed during indexing preparation')
            result = apply_decision(store, current, 'approve', automatic=True)
            store.db.execute('UPDATE batches SET error=NULL WHERE id=?', (identifier,))
            store.db.commit()
            return result
        except Exception as error:
            store.db.rollback()
            detail = str(error) if isinstance(error, CrawlError) else 'Indexing preparation failed; saved files are retained. Retry preparation.'
            store.db.execute("UPDATE batches SET state='index_preflight_failed',error=?,updated=? WHERE id=? AND state='awaiting_review' AND manifest_sha256=?",
                             (detail, now(), identifier, expected))
            store.db.commit()


def retry(root, identifier, expected):
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        captured = get(store, valid_id(identifier))
        if captured['state'] != 'index_preflight_failed' or captured['manifest_sha256'] != expected:
            raise CrawlError('This preparation failure changed or is already queued for retry')
        store.db.execute("UPDATE batches SET state='awaiting_review',error=NULL,updated=? WHERE id=?", (now(), identifier))
        store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                         (captured['manifest']['sites'][0]['id'], 'indexing_preparation_retried',
                          json.dumps({'id': identifier, 'manifest_sha256': expected}), now()))
        store.db.commit()
    return {'state': 'awaiting_review'}
