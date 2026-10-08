import json
from unittest.mock import patch

import pytest

from common import digest
from conftest import add_candidate, manifest_for
from server import create_app
from state import connect, unpack
from test_server import call
from test_site_reviews import mixed_sites, review_for, decision


def snapshot(app, query=''):
    return call(app, 'GET', '/api/queue?filter=review' + query).json()['review_actions']


def bulk(app, token, action):
    return call(app, 'POST', '/api/review-decisions', {'token': token, 'decision': action})


def test_bulk_review_approves_independent_complete_sites_and_never_other_stages(tmp_path):
    root = tmp_path / 'state'
    rows, parent = mixed_sites(root)
    untouched = add_candidate(root, url='http://untouched.example/')
    app = create_app(root, start_worker=False)
    current = snapshot(app, '&search=guild')
    assert current['count'] == 2 and current['files'] == 3 and current['max_enrichment_usd'] == 4
    assert bulk(app, current['token'], 'approve').status_code == 202
    assert snapshot(app)['count'] == 0
    assert bulk(app, current['token'], 'approve').status_code == 409
    with connect(root) as store:
        operations = [unpack(row) for row in store.db.execute('SELECT * FROM operations')]
        assert len(operations) == 2 and all(op['kind'] == 'publish' for op in operations)
        manifests = []
        for operation in operations:
            batch = unpack(store.db.execute('SELECT * FROM batches WHERE id=?', (operation['payload']['batch_id'],)).fetchone())
            assert operation['payload']['manifest_sha256'] == digest(batch['manifest'])
            assert batch['state'] == 'publication_requested'
            assert len(batch['manifest']['sites']) == 1
            manifests.extend(batch['manifest']['captures'])
        assert sorted(item['path'] for item in manifests) == sorted(item['path'] for item in parent['captures'])
        assert store.db.execute('SELECT state FROM candidates WHERE id=?', (untouched['id'],)).fetchone()[0] == 'approval_pending'


def test_bulk_approval_validates_every_source_and_fails_without_partial_approvals(tmp_path):
    root = tmp_path / 'state'
    rows, parent = mixed_sites(root)
    app = create_app(root, start_worker=False)
    current = snapshot(app)
    (root / parent['captures'][-1]['path']).write_bytes(b'tampered')
    response = bulk(app, current['token'], 'approve')
    assert response.status_code == 409 and 'No sites were approved' in response.json()['error']
    assert snapshot(app) == current
    with connect(root) as store:
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()
    # A broken source must not trap sites in Review.
    assert bulk(app, current['token'], 'decline').status_code == 200


def test_bulk_review_rechecks_snapshot_after_slow_source_validation(tmp_path):
    import review_actions
    root = tmp_path / 'state'
    rows, parent = mixed_sites(root)
    app = create_app(root, start_worker=False)
    current = snapshot(app)
    validate = review_actions.check_manifest
    changed = False
    def race(root, manifest):
        nonlocal changed
        validate(root, manifest)
        if not changed:
            changed = True
            # A concurrent writer can proceed during validation (no long lock).
            with connect(root) as store:
                store.db.execute("UPDATE candidates SET scope=scope || 'changed/' WHERE id=?", (rows[0]['id'],))
                store.db.commit()
    with patch('review_actions.check_manifest', race):
        assert bulk(app, current['token'], 'approve').status_code == 409
    with connect(root) as store:
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()


def test_review_bulk_preview_and_undo_are_not_limited_by_pages_or_batch_inventory(tmp_path):
    root = tmp_path / 'state'
    for index in range(105):
        row = add_candidate(root, url=f'http://guild-{index}.example/')
        manifest = manifest_for(row, batch_id=f'{index:032x}')
        with connect(root) as store:
            store.db.execute("INSERT INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,'now','now')", (manifest['batch_id'], json.dumps(manifest), digest(manifest)))
            store.db.execute("UPDATE candidates SET state='captured_awaiting_review' WHERE id=?", (row['id'],))
            store.db.commit()
    app = create_app(root, start_worker=False)
    # Polling only reads metadata; it must not hash/read the sources.
    with patch('review_actions.check_manifest', side_effect=AssertionError('metadata only')):
        current = snapshot(app, '&search=guild-104&offset=50')
    assert current['count'] == 105 and current['files'] == 105
    dismissed = bulk(app, current['token'], 'decline').json()
    assert dismissed['count'] == 105 and snapshot(app)['count'] == 0
    assert call(app, 'GET', '/api/queue?filter=history').json()['total'] == 105
    assert call(app, 'POST', '/api/undo-review-dismissal', {'dismissal': dismissed['dismissal']}).json() == {'restored': 105}
    assert snapshot(app)['count'] == 105
    assert call(app, 'POST', '/api/undo-review-dismissal', {'dismissal': dismissed['dismissal']}).status_code == 409


def test_bulk_undo_rejects_intervening_individual_decision_atomically(tmp_path):
    root = tmp_path / 'state'
    rows, parent = mixed_sites(root)
    app = create_app(root, start_worker=False)
    reviews = [review_for(app, row) for row in rows]
    dismissed = bulk(app, snapshot(app)['token'], 'decline').json()
    assert decision(app, reviews[0], 'reconsider').status_code == 200
    assert decision(app, reviews[0], 'decline').status_code == 200
    assert call(app, 'POST', '/api/undo-review-dismissal', {'dismissal': dismissed['dismissal']}).status_code == 409
    assert snapshot(app)['count'] == 0
    assert call(app, 'GET', '/api/queue?filter=history').json()['total'] == 2


@pytest.mark.parametrize('payload', [{}, [], {'token': 'x', 'decision': 'reconsider'}, {'token': 2, 'decision': 'approve'}, {'token': 'x', 'decision': ['approve']}])
def test_bulk_review_rejects_invalid_payloads(tmp_path, payload):
    app = create_app(tmp_path / 'state', start_worker=False)
    assert call(app, 'POST', '/api/review-decisions', payload).status_code == 409
    assert call(app, 'POST', '/api/undo-review-dismissal', payload).status_code == 409
