import json
from concurrent.futures import ThreadPoolExecutor

from conftest import add_candidate
from server import create_app
from state import connect
from test_server import call


def test_minimum_grade_sorting_and_ungraded_filter_precede_pagination(tmp_path):
    root = tmp_path / 'state'
    for grade in (0, 1, 2, 3, None):
        row = add_candidate(root, url=f'http://grade-{grade}.example/', grade=grade)
        with connect(root) as store:
            store.db.execute('UPDATE candidates SET priority=? WHERE id=?', (1000 if grade == 2 else 1, row['id']))
            store.db.commit()
    app = create_app(root, start_worker=False)
    all_rows = call(app, 'GET', '/api/queue?filter=candidates').json()
    assert [(row['rating'] or {}).get('grade') for row in all_rows['candidates']] == [3, 2, 1, 0, None]
    selected = call(app, 'GET', '/api/queue?filter=candidates&min_grade=2').json()
    assert [row['rating']['grade'] for row in selected['candidates']] == [3, 2]
    assert selected['stage_counts']['candidates'] == selected['dismissal']['count'] == 5
    assert selected['candidate_grades'] == {'-1': 1, '0': 1, '1': 1, '2': 1, '3': 1}
    assert call(app, 'GET', '/api/queue?filter=candidates&min_grade=0').json()['total'] == 4
    pending = call(app, 'GET', '/api/queue?filter=candidates&min_grade=2&needs_grade=1').json()
    assert pending['total'] == 1 and pending['candidates'][0]['rating'] is None
    assert call(app, 'GET', '/api/queue?filter=candidates&min_grade=3&offset=1').json()['candidates'] == []
    assert call(app, 'GET', '/api/queue?filter=candidates&min_grade=2&search=grade-0').json()['total'] == 0
    # Existing operator filters retain their contracts.
    assert call(app, 'GET', '/api/queue?filter=all&min_grade=3').json()['total'] == 5
    for query in ('min_grade=-1', 'min_grade=4', 'min_grade=2.0', 'needs_grade=yes'):
        assert call(app, 'GET', '/api/queue?filter=candidates&' + query).status_code == 409
    with connect(root) as store:
        original = dict(store.db.execute("SELECT * FROM candidates WHERE url='http://grade-0.example/'").fetchone())
        for index in range(60):
            clone = {**original, 'id': f'{index:024x}', 'url': f'http://low-{index}.example/', 'priority': 2000}
            columns = list(clone)
            store.db.execute(f"INSERT INTO candidates({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", list(clone.values()))
        store.db.commit()
    selected = call(app, 'GET', '/api/queue?filter=candidates&min_grade=2').json()
    assert selected['total'] == 2 and [row['rating']['grade'] for row in selected['candidates']] == [3, 2]
    assert call(app, 'GET', '/api/queue?filter=candidates&min_grade=0').json()['candidates'][0]['rating']['grade'] == 3


def test_dismiss_all_includes_hidden_candidates_beyond_both_page_and_decision_caps(candidate):
    root, row = candidate
    with connect(root) as store:
        original = dict(store.candidates()[0])
        for index in range(505):
            clone = {**original, 'id': f'{index:024x}', 'url': f'http://hidden-{index}.example/',
                     'state': ['grade_error', 'coverage_unverified', 'unavailable'][index % 3], 'rating': None, 'captures': '[]'}
            columns = list(clone)
            store.db.execute(f"INSERT INTO candidates({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", list(clone.values()))
        store.db.commit()
    queued = add_candidate(root, url='http://already-queued.example/')
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='approved_waiting_batch' WHERE id=?", (queued['id'],))
        store.db.commit()
    app = create_app(root, start_worker=False)
    listing = call(app, 'GET', '/api/queue?filter=candidates&min_grade=3&search=guild').json()
    assert listing['total'] == 1 and listing['dismissal']['count'] == 506
    payload = {'token': listing['dismissal']['token']}
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: call(app, 'POST', '/api/dismiss-candidates', payload), range(2)))
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    outcome = next(reply.json() for reply in replies if reply.status_code == 200)
    assert outcome['dismissed'] == 506
    assert call(app, 'GET', '/api/queue?filter=candidates').json()['total'] == 0
    assert call(app, 'GET', '/api/queue?filter=queued').json()['total'] == 1
    assert call(app, 'GET', '/api/queue?filter=history').json()['total'] == 506
    undo = call(app, 'POST', '/api/undo-dismissal', {'dismissal': outcome['dismissal']})
    assert undo.status_code == 200 and undo.json()['restored'] == 506
    assert call(app, 'POST', '/api/undo-dismissal', {'dismissal': outcome['dismissal']}).status_code == 409
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 0


def test_dismiss_all_and_undo_reject_stale_snapshots_atomically(candidate):
    root, row = candidate
    other = add_candidate(root, url='http://other.example/')
    app = create_app(root, start_worker=False)
    token = call(app, 'GET', '/api/queue?filter=candidates').json()['dismissal']['token']
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='approved_waiting_batch' WHERE id=?", (other['id'],))
        store.db.commit()
    assert call(app, 'POST', '/api/dismiss-candidates', {'token': token}).status_code == 409
    assert call(app, 'GET', '/api/queue?filter=history').json()['total'] == 0
    token = call(app, 'GET', '/api/queue?filter=candidates').json()['dismissal']['token']
    result = call(app, 'POST', '/api/dismiss-candidates', {'token': token}).json()
    assert call(app, 'POST', '/api/restore', {'id': row['id'], 'manifest_sha256': row['manifest_sha256']}).status_code == 200
    assert call(app, 'POST', '/api/undo-dismissal', {'dismissal': result['dismissal']}).status_code == 409
    for path, payload in [('/api/dismiss-candidates', {}), ('/api/undo-dismissal', {}), ('/api/undo-dismissal', {'dismissal': '../'})]:
        assert call(app, 'POST', path, payload).status_code == 409
    assert call(app, 'POST', '/api/dismiss-candidates', {'token': token}, peer='203.0.113.10').status_code == 403


def test_unverified_dismissals_stay_in_history_during_coverage_polls(candidate, monkeypatch, tmp_path):
    from test_coverage_check import archive
    from site_inventory import SiteInventory
    root, row = candidate
    repo = archive(tmp_path, ['known.example/20000101000000/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO', str(repo))
    monkeypatch.setattr(SiteInventory, 'check', lambda *args, **kwargs: {'status': 'inventory_partial', 'complete': False})
    app = create_app(root, start_worker=False)
    item = call(app, 'GET', '/api/queue?filter=candidates').json()['candidates'][0]
    assert item['state'] == 'coverage_unverified'
    result = call(app, 'POST', '/api/decisions', [{'id': row['id'], 'manifest_sha256': row['manifest_sha256'], 'decision': 'reject'}])
    assert result.status_code == 200
    assert call(app, 'GET', '/api/queue?filter=candidates').json()['total'] == 0
    assert call(app, 'GET', '/api/queue?filter=history').json()['candidates'][0]['state'] == 'rejected'
