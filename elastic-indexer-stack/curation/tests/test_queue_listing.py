"""Large, actively changing queues must remain cheap and consistent to browse."""
import json

import pytest

from conftest import add_candidate
from server import create_app
from state import connect
from test_server import call


@pytest.mark.parametrize('selected', ['recommended', 'pending', 'approved', 'captured', 'review', 'all',
                                     'candidates', 'candidates&min_grade=2', 'candidates&needs_grade=1',
                                     'candidates&search=EQ%20Guild', 'saved', 'queued', 'history'])
def test_compact_listing_preserves_review_records_counts_filters_and_action_tokens(tmp_path, selected):
    root = tmp_path / 'state'
    for index, grade in enumerate((None, 0, 1, 2, 3)):
        add_candidate(root, url=f'http://site-{index}.example/', grade=grade)
    app = create_app(root, start_worker=False)
    original = call(app, 'GET', '/api/queue?filter=' + selected).json()
    compact = call(app, 'GET', '/api/queue?compact=1&filter=' + selected).json()
    for key in ('candidates', 'total', 'offset', 'stage_counts', 'candidate_grades', 'dismissal', 'review_actions',
                'all_count', 'approved', 'recommended', 'capturing', 'captured'):
        assert compact[key] == original[key], key


@pytest.mark.parametrize('stage', ['candidates', 'queued', 'capturing', 'indexing', 'saved', 'history'])
def test_portal_poll_reads_saved_metadata_without_running_archive_maintenance(candidate, monkeypatch, stage):
    root, _ = candidate
    app = create_app(root, start_worker=False)

    def maintenance(*args, **kwargs):
        pytest.fail('A passive portal poll must not run archive checks or candidate maintenance')

    monkeypatch.setattr('server.refresh', maintenance)
    assert call(app, 'GET', f'/api/queue?compact=1&filter={stage}').status_code == 200


def test_large_queue_only_prepares_visible_review_records(candidate, monkeypatch):
    root, seed = candidate
    with connect(root) as store:
        original = dict(store.candidates()[0])
        values = []
        for index in range(4000):
            row = {**original, 'id': f'{index:024x}', 'url': f'http://guild-{index:04}.example/',
                   'state': 'approved_waiting_batch' if index < 700 else 'deferred',
                   'decision': json.dumps({'decision': 'approve', 'reviewed_at': '2026-01-01T00:00:00Z'})}
            values.append(list(row.values()))
        store.db.executemany(f"INSERT INTO candidates({','.join(original)}) VALUES ({','.join('?' for _ in original)})", values)
        store.db.commit()
    app = create_app(root, start_worker=False)
    import review
    real_record = review.record
    prepared = []

    def prepare(store, row):
        prepared.append(row['id'])
        assert len(prepared) <= 50, 'Off-page sites must not run source/scope validation'
        return real_record(store, row)

    monkeypatch.setattr('review.record', prepare)
    monkeypatch.setattr('server.record', prepare)
    for offset in (0, 650):
        prepared.clear()
        result = call(app, 'GET', f'/api/queue?compact=1&filter=queued&offset={offset}').json()
        ids = [f'{index:024x}' for index in range(offset, offset + 50)]
        assert [row['id'] for row in result['candidates']] == ids
        assert prepared == ids
        assert result['total'] == result['stage_counts']['queued'] == 700
        assert result['all_count'] == 4001 and result['stage_counts']['saved'] == 3300
        assert result['dismissal']['count'] == 1
    prepared.clear()
    result = call(app, 'GET', '/api/queue?compact=1&filter=queued&search=GUILD-0699').json()
    assert prepared == [f'{699:024x}']
    assert result['total'] == 1 and result['stage_counts']['queued'] == 700
    assert result['candidates'][0]['evidence'] == seed['evidence']


def test_claim_during_listing_cannot_remove_a_queue_sort_key(candidate, monkeypatch):
    root, row = candidate
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='approved_waiting_batch',decision=? WHERE id=?",
                         (json.dumps({'decision': 'approve', 'reviewed_at': '2026-01-01T00:00:00Z'}), row['id']))
        store.db.commit()
    app = create_app(root, start_worker=False)
    import server
    order = server.queue_order

    def concurrent_claim(store):
        with connect(root) as worker:
            worker.db.execute("UPDATE candidates SET state='capturing' WHERE id=?", (row['id'],))
            worker.db.commit()
        return order(store)

    monkeypatch.setattr('server.queue_order', concurrent_claim)
    first = call(app, 'GET', '/api/queue?compact=1&filter=queued').json()
    assert first['total'] == first['stage_counts']['queued'] == 1
    assert first['candidates'][0]['state'] == 'approved_waiting_batch'
    second = call(app, 'GET', '/api/queue?compact=1&filter=queued').json()
    assert second['total'] == 0 and second['stage_counts']['capturing'] == 1


def test_approval_still_rechecks_archive_after_a_fast_portal_read(candidate, tmp_path, monkeypatch):
    from test_coverage_check import archive
    root, row = candidate
    repo = archive(tmp_path, ['guild.example/20000101000000/eq/news.html'])
    monkeypatch.setenv('ARCHIVE_REPO', str(repo))
    app = create_app(root, start_worker=False)
    listing = call(app, 'GET', '/api/queue?compact=1&filter=candidates').json()
    assert listing['candidates'][0]['state'] == 'approval_pending'
    response = call(app, 'POST', '/api/decisions', [{'id': row['id'],
                    'manifest_sha256': listing['candidates'][0]['manifest_sha256'], 'decision': 'approve'}])
    assert response.status_code == 409
    with connect(root) as store:
        assert store.candidates()[0]['state'] == 'already_archived'
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()
