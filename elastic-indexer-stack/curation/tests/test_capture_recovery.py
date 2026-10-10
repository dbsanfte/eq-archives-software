import json
from contextlib import closing
from pathlib import Path

import pytest

import capture_recovery
from captures import capture_sites
from common import CrawlError, digest
from conftest import manifest_for, add_candidate
from server import create_app
from state import connect, unpack
from test_capture_resume import interrupted
from test_capture_queue import approve, make_due
from test_server import call
from worker import Worker


@pytest.mark.parametrize('failure', ['Wayback connection failed after bounded retries',
                                    'Wayback HTTP 503 after bounded retries'])
def test_bad_file_retries_are_bounded_and_other_files_finish(candidate, failure):
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': 'complete-files-v1'}
    calls = []
    class Downloader:
        def __init__(self, *args): pass
        def close(self): pass
        def call(self, job):
            calls.append((job['op'], job['url']))
            if job['op'] == 'scope_list':
                return {'captures': [{'url': site['scope'] + name, 'timestamp': '20000101000000'}
                                     for name in ('bad.png', 'good.png')], 'resume_key': None}
            if job['url'].endswith('bad.png'):
                raise CrawlError(failure)
            Path(job['destination']).write_bytes(b'image')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': 5,
                    'sha256': digest(b'image'), 'content_type': 'image/png'}
    for attempt in range(2):
        with pytest.raises(CrawlError, match=failure):
            capture_sites(root, 'a' * 32, [site], Downloader)
        assert calls.count(('capture_file', site['scope'] + 'good.png')) == 1
    manifest = capture_sites(root, 'a' * 32, [site], Downloader)
    assert len(manifest['captures']) == 2
    assert manifest['capture_coverage'][site['id']]['state'] == 'complete_with_gaps'
    assert manifest['capture_retry'] == {'files': 1, 'lookups': 0}
    assert manifest['notes'][0]['url'] == site['scope'] + 'bad.png'
    assert calls.count(('capture_file', site['scope'] + 'bad.png')) == 3
    assert sum(op == 'scope_list' for op, _ in calls) == 1


def test_existing_transport_failure_requeues_without_enabling_automatic_mode(candidate, monkeypatch):
    root, row = candidate
    app = create_app(root, start_worker=False)
    op, payload, progress = interrupted(root, app, row)
    monkeypatch.setattr(capture_recovery, 'now', lambda: '2026-10-10T00:00:00+00:00')
    capture_recovery.schedule(root)
    with connect(root) as store:
        retry = capture_recovery.detail(store, op)
        assert retry['retry_at'] == '2026-10-10T00:05:00+00:00'
        assert store.db.execute('SELECT state FROM operations WHERE id=?', (op,)).fetchone()[0] == 'interrupted'
    other = add_candidate(root, url='http://other.example/')
    approve(app, other)
    make_due(root)
    worker = Worker(root)
    try:
        worker.capture_queue()
        monkeypatch.setattr(capture_recovery, 'now', lambda: '2026-10-10T00:05:00+00:00')
        capture_recovery.schedule(root)
        with connect(root) as store:
            saved = unpack(store.db.execute('SELECT * FROM operations WHERE id=?', (op,)).fetchone())
            assert saved['state'] == 'resume_queued'
            assert saved['payload'] == payload and saved['result'] == progress
            assert store.db.execute("SELECT COUNT(*) FROM operations WHERE state='queued'").fetchone()[0] == 1
        assert call(app, 'GET', '/api/queue?filter=queued').json()['candidates'][0]['id'] == row['id']
    finally:
        worker.close()


def test_human_cancel_and_integrity_errors_are_never_automatically_resumed(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    op, _, _ = interrupted(root, app, row)
    call(app, 'POST', '/api/resume', {'id': op})
    call(app, 'POST', '/api/cancel-resume', {'id': op})
    capture_recovery.schedule(root)
    with connect(root) as store:
        assert not capture_recovery.detail(store, op) or capture_recovery.detail(store, op)['paused']
        store.db.execute("UPDATE operations SET error='Staged capture failed source integrity checks' WHERE id=?", (op,))
        store.db.commit()
    capture_recovery.schedule(root)
    assert call(app, 'GET', '/api/candidate?id=' + row['id']).json()['capture_operation']['state'] == 'interrupted'


def test_worker_restart_retains_checkpoint_and_schedules_approved_capture(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    op, payload, progress = interrupted(root, app, row)
    with connect(root) as store:
        store.db.execute("UPDATE operations SET state='running' WHERE id=?", (op,))
        store.db.commit()
    worker = Worker(root)
    try:
        worker.capture_queue()
        current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['capture_operation']
        assert current['state'] == 'interrupted' and current['retry']['retry_at']
        assert 'retry automatically' in current['error']
        assert current['payload'] == payload and current['result'] == progress
    finally:
        worker.close()


def test_worker_backoff_is_durable_pause_is_explicit_and_retry_now_joins_queue(candidate, monkeypatch):
    root, row = candidate
    app = create_app(root, start_worker=False)
    op, _, _ = interrupted(root, app, row)
    monkeypatch.setattr(capture_recovery, 'now', lambda: '2026-10-10T00:00:00+00:00')
    worker = Worker(root)
    def fail(*args, **kwargs):
        raise CrawlError('Wayback HTTP 429 after bounded retries')
    monkeypatch.setattr('worker.capture_sites', fail)
    try:
        for expected in ('00:05:00', '00:15:00', '01:00:00', '01:00:00'):
            assert call(app, 'POST', '/api/resume', {'id': op}).status_code == 202
            worker.capture_queue();worker.operation()
            data = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
            retry = data['capture_operation']['retry']
            assert retry['retry_at'] == f'2026-10-10T{expected}+00:00'
            listing = call(app, 'GET', '/api/queue?filter=capturing').json()
            assert listing['capture_attention']['retrying'] == 1
            assert listing['capture_attention']['interrupted'] == 0
            assert listing['activity']['next_retry']['id'] == row['id']
        assert call(app, 'POST', '/api/pause-capture-retries', {'id': op}).status_code == 200
        monkeypatch.setattr(capture_recovery, 'now', lambda: '2026-10-11T00:00:00+00:00')
        worker.capture_queue()
        assert call(app, 'GET', '/api/candidate?id=' + row['id']).json()['capture_operation']['state'] == 'interrupted'
        assert call(app, 'POST', '/api/pause-capture-retries', {'id': op}).status_code == 409
        assert call(app, 'POST', '/api/pause-capture-retries', {}).status_code == 409
        assert call(app, 'POST', '/api/resume', {'id': op}).status_code == 202
        worker.capture_queue();worker.operation()
        assert not call(app, 'GET', '/api/candidate?id=' + row['id']).json()['capture_operation']['retry']['paused']
    finally:
        worker.close()


@pytest.mark.parametrize('platform', ['ezboard', 'sitepowerup'])
@pytest.mark.parametrize('failure', ['Wayback HTTP 403', 'Wayback connection failed after bounded retries'])
@pytest.mark.parametrize('owned', [False, True])
def test_board_gaps_finish_and_history_retry_fetches_only_missing_page(tmp_path, monkeypatch, platform, failure, owned):
    from capture_continuation import retryable
    from captures import check_manifest
    if platform == 'ezboard':
        from test_ezboard import BOARD, FORUM as PAGE, STAMP, html
        board_html, page_html = html(PAGE), html(BOARD)
    else:
        from test_sitepowerup import BOARD, MESSAGE as PAGE, STAMP, html
        board_html, page_html = html(), html(reply='12155')
    monkeypatch.delenv('ARCHIVE_REPO', raising=False)
    root = tmp_path / 'state'
    row = add_candidate(root, url=BOARD, body=board_html)
    site = {**manifest_for(row)['sites'][0], 'scope': BOARD, 'scope_mode': platform, 'capture_policy': 'complete-files-v1'}
    calls, broken = [], True
    class Downloader:
        def __init__(self, store, args): self.store = store
        def close(self): pass
        def call(self, job):
            calls.append((job['op'], job['url']))
            usage = self.store.get('wayback_transport', {})
            self.store.set('wayback_transport', {**usage, 'requests': usage.get('requests', 0) + 1})
            if job['op'].endswith('_list'):
                return {'captures': [{'url': PAGE, 'timestamp': STAMP, 'digest': 'D', 'length': '20'}], 'resume_key': None}
            if broken: raise CrawlError(failure)
            Path(job['destination']).write_bytes(page_html)
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': len(page_html),
                    'sha256': digest(page_html), 'content_type': 'text/html'}
    batch = 'b' * 32
    if 'connection' in failure:
        for _ in range(2):
            with pytest.raises(CrawlError, match='connection'):
                capture_sites(root, batch, [site], Downloader)
    original = capture_sites(root, batch, [site], Downloader)
    assert original['capture_retry'] == {'files': 1, 'lookups': 0}
    assert retryable(original)
    assert len(original['captures']) == 1
    assert original['capture_coverage'][row['id']]['state'] == 'complete_with_gaps'
    if platform == 'ezboard' and failure == 'Wayback HTTP 403':
        # Completed checkpoints from before board gaps entered the all-file
        # catalog retain their original notes and immutable manifest.
        from common import Store
        original['capture_retry']['files'] = 0
        original['notes'] = [{'url': PAGE, 'stamp': STAMP, 'state': 'unavailable', 'reason': failure}]
        with closing(Store(root/'batches'/batch/'complete')) as checkpoint:
            checkpoint.db.execute("DELETE FROM full_records WHERE state='unavailable'")
            checkpoint.db.commit()
        (root/'batches'/batch/'complete'/'manifest.json').write_text(json.dumps(original))
        assert retryable(original)
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'capture_continued',?,?,NULL,NULL,NULL,'2026','2026')",
                         (batch, json.dumps(original), digest(original)))
        store.db.commit()
    retry_site = {**site, 'continued_from': {'review_id': batch, 'manifest_sha256': digest(original), 'mode': 'retry_failed'}}
    broken = False;calls.clear()
    if not owned:
        page_html = html('http://pub4.ezboard.com/bother') if platform == 'ezboard' else html('999')
    recovered = capture_sites(root, 'c' * 32, [retry_site], Downloader)
    assert calls == [('capture_file', PAGE)]
    assert len(recovered['captures']) == (2 if owned else 1) and recovered['captures'][0] == original['captures'][0]
    assert recovered['capture_retry'] == {'files': 0 if owned else 1, 'lookups': 0}
    assert recovered[platform]['coverage']['counts']['unavailable'] == (0 if owned else 1)
    assert recovered['transport']['requests'] == original['transport']['requests'] + 1
    assert json.loads((root/'batches'/batch/'complete'/'manifest.json').read_text()) == original
    check_manifest(root, recovered)


def test_out_of_window_catalog_row_cannot_stall_in_scope_files(candidate):
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': 'complete-files-v1'}
    class Downloader:
        def __init__(self, *args): pass
        def close(self): pass
        def call(self, job):
            if job['op'] == 'scope_list':
                return {'captures': [{'url': site['scope']+'picture.png', 'timestamp': stamp}
                                     for stamp in ('20070101000000','20061231235959')]}
            assert job['timestamp'] == '20061231235959'
            Path(job['destination']).write_bytes(b'png')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': 3,
                    'sha256': digest(b'png'), 'content_type': 'image/png'}
    manifest = capture_sites(root, 'f' * 32, [site], Downloader)
    assert len(manifest['captures']) == 2
    assert manifest['capture_retry'] == {'files': 0, 'lookups': 0}
    assert manifest['notes'][0]['kind'] == 'outside_date_window'


@pytest.mark.parametrize('failure_at', ['file', 'supporting_lookup', 'primary_inventory'])
def test_automatic_recovery_advances_gaps_to_indexing_but_never_skips_primary_inventory(tmp_path, monkeypatch, failure_at):
    from automatic_indexing import prepare_next
    root = tmp_path / 'state'
    asset = 'https://static.example/missing.png'
    body = b'<p>EverQuest guild history.</p>'
    if failure_at == 'supporting_lookup':
        body += f'<img src="{asset}">'.encode()
    row = add_candidate(root, body=body)
    app = create_app(root, start_worker=False)
    assert approve(app, row).status_code == 200
    make_due(root)
    calls = []
    class Downloader:
        def __init__(self, *args): pass
        def close(self): pass
        def call(self, job):
            calls.append((job['op'], job['url']))
            if ((failure_at == 'primary_inventory' and job['op'] == 'scope_list')
                    or (failure_at == 'supporting_lookup' and job['url'] == asset)
                    or (failure_at == 'file' and job['url'].endswith('missing.png'))):
                raise CrawlError('Wayback HTTP 503 after bounded retries')
            if job['op'] == 'scope_list':
                names = ['good.png', 'missing.png'] if failure_at == 'file' else ['good.png']
                return {'captures': [{'url': row['scope'] + name, 'timestamp': '20000101000000'} for name in names]}
            Path(job['destination']).write_bytes(b'png')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': 3,
                    'sha256': digest(b'png'), 'content_type': 'image/png'}
    monkeypatch.setattr('worker.capture_sites', lambda root, batch, sites, progress:
                        capture_sites(root, batch, sites, Downloader, progress))
    worker = Worker(root)
    try:
        for attempt in range(3):
            worker.capture_queue(); worker.operation()
            data = call(app, 'GET', '/api/candidate?id=' + row['id']).json()
            if attempt < 2 or failure_at == 'primary_inventory':
                assert data['candidate']['stage'] == 'capturing'
                assert data['capture_operation']['retry']['retry_at']
                assert prepare_next(root) is None
                with connect(root) as store:
                    store.db.execute("UPDATE capture_retries SET retry_at='1999-01-01'")
                    store.db.commit()
            else:
                assert data['candidate']['stage'] == 'indexing'
                assert data['capture_operation']['state'] == 'completed'
                manifest = data['review']['manifest']
                assert len(manifest['captures']) == 2
                assert manifest['capture_retry'] == {'files': int(failure_at == 'file'), 'lookups': int(failure_at == 'supporting_lookup')}
                assert prepare_next(root)['operation']
                assert call(app, 'GET', '/api/candidate?id=' + row['id']).json()['review']['state'] == 'publication_requested'
        if failure_at != 'primary_inventory':
            assert calls.count(('capture_file', row['scope'] + 'good.png')) == 1
            assert calls.count(('scope_list', row['scope'].rstrip('/'))) == 1
        else:
            assert all(op == 'scope_list' for op, _ in calls)
    finally:
        worker.close()


def test_file_failure_streak_yields_and_does_not_skip_rate_limited_urls(candidate):
    from capture_failures import FileRetries, WORKER_INTERRUPTED
    root, _ = candidate
    with connect(root) as store:
        attempts = FileRetries(store)
        for error in (RuntimeError('bug'), CrawlError('Wayback HTTP 429 after bounded retries'),
                      CrawlError('Worker stopped; resume explicitly'), CrawlError(WORKER_INTERRUPTED), CrawlError('Staged source hash changed')):
            assert attempts.failed('file', error) is None
        assert not store.db.execute('SELECT * FROM capture_file_attempts').fetchone()
        assert attempts.failed('file', CrawlError('Wayback HTTP 403')) == 'unavailable'
        assert [attempts.failed('file', CrawlError('Wayback HTTP 502')) for _ in range(3)] == ['retry','retry','unavailable']
        assert attempts.streak == 3
        attempts.succeeded()
        assert attempts.streak == 0
