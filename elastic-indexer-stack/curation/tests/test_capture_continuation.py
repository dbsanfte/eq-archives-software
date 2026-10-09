import json
from pathlib import Path
from unittest.mock import patch

import pytest

from common import CAPTURE_WINDOW, CrawlError, digest
from captures import archive_path, capture_sites, check_manifest
from conftest import add_candidate, manifest_for
from full_capture import POLICY
from review import apply_decisions
from server import create_app
from state import connect, unpack
from test_capture_queue import make_due
from test_server import call
from worker import Worker


def legacy(root, count=2, identifier='a' * 32, url='http://guild.example/', coverage='bounded'):
    row = add_candidate(root, url=url)
    with connect(root) as store:
        apply_decisions(store, [{'id': row['id'], 'manifest_sha256': row['manifest_sha256'], 'decision': 'approve'}])
        grant = json.loads(store.db.execute('SELECT decision FROM candidates WHERE id=?', (row['id'],)).fetchone()[0])
    manifest = manifest_for(row, identifier)
    manifest['sites'][0]['decision'] = grant
    manifest.update(limits={'files': 100}, capture_window=dict(CAPTURE_WINDOW),
                    capture_coverage={row['id']: {'state': coverage, 'reason': 'Legacy traversal'}},
                    transport={'requests': 105, 'bytes': 1665097, 'seconds': 355.5, 'connections': 1})
    for index in range(1, count):
        capture = {**manifest['captures'][0], 'url': url + f'page{index}.html'}
        capture['archive_path'] = archive_path(capture)
        manifest['captures'].append(capture)
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,?,?)",
                         (identifier, json.dumps(manifest), digest(manifest), '2026-01-01', '2026-01-01'))
        store.db.execute("UPDATE candidates SET state='captured_awaiting_review',coverage=? WHERE id=?",
                         (json.dumps({'status': 'absent_host', 'capture': {'batch_id': identifier}}), row['id']))
        store.db.commit()
    return row, manifest


def regenerate(app, manifest):
    return call(app, 'POST', '/api/continue-capture', {'id': manifest['batch_id'], 'manifest_sha256': digest(manifest)})


def detail(app, row):
    return call(app, 'GET', '/api/candidate?id=' + row['id']).json()


@pytest.mark.parametrize('with_exclusion', [False, True])
def test_retry_failed_files_retains_review_and_retries_only_gaps(tmp_path, monkeypatch, with_exclusion):
    root = tmp_path / 'state'
    row, legacy_manifest = legacy(root, count=1)
    site = {**legacy_manifest['sites'][0], 'capture_policy': POLICY}
    seed = site['captures'][0]
    body = b'<p>EQ</p><img src="http://counter.example/i.gif">'
    if with_exclusion:
        body += b'<img src="http://ads.example/banner.gif">'
    (root / seed['path']).write_bytes(body)
    seed.update(bytes=len(body), sha256=digest(body))
    failing = True
    calls = []
    class Downloader:
        def __init__(self, store, args): self.store = store
        def call(self, job):
            assert 'ads.example' not in job['url']
            calls.append((job['op'], job['url']))
            previous = self.store.get('wayback_transport', {})
            self.store.set('wayback_transport', {**previous, 'requests': previous.get('requests', 0) + 1})
            if failing and ('counter.example' in job['url'] or job['op'] == 'capture_file' and job['url'].endswith('bad.png')):
                raise CrawlError('Wayback HTTP 403')
            if job['op'] == 'scope_list':
                urls = [job['url']] if 'counter.example' in job['url'] else [row['scope'] + name for name in ('bad.png', 'good.png')]
                return {'captures': [{'url': url, 'timestamp': '20061231235959'} for url in urls]}
            Path(job['destination']).write_bytes(b'PNG')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': 3,
                    'sha256': digest(b'PNG'), 'content_type': 'image/png'}
        def close(self): pass
    original = capture_sites(root, legacy_manifest['batch_id'], [site], Downloader)
    original['indexing']['max_enrichment_usd'] = .5
    (root / 'batches' / original['batch_id'] / 'complete' / 'manifest.json').write_text(json.dumps(original))
    assert original['capture_retry'] == {'files': 1, 'lookups': 1}
    assert len(original['captures']) == 2  # The later good file survived the 403.
    with connect(root) as store:
        store.db.execute('UPDATE batches SET manifest=?,manifest_sha256=? WHERE id=?',
                         (json.dumps(original), digest(original), original['batch_id']))
        store.db.commit()
    app = create_app(root, start_worker=False)
    monkeypatch.setattr('worker.capture_sites', lambda root, batch, sites, progress: capture_sites(root, batch, sites, Downloader, progress))
    worker = Worker(root)
    try:
        assert regenerate(app, original).status_code == 202
        assert detail(app, row)['candidate']['coverage']['capture']['continuation']['mode'] == 'retry_failed'
        assert call(app, 'POST', '/api/undo', {'id': row['id'], 'manifest_sha256': row['manifest_sha256']}).status_code == 200
        assert detail(app, row)['review']['manifest'] == original
        for recovered in (False, True):
            before = detail(app, row)['review']['manifest']
            assert regenerate(app, before).status_code == 202
            assert regenerate(app, before).status_code == 409
            make_due(root);worker.capture_queue()
            failing = not recovered;calls.clear()
            worker.operation()
            after = detail(app, row)
            assert after['candidate']['stage'] == 'indexing', after['capture_operation']
            result = after['review']['manifest']
            retained = {c['url']: c for c in result['captures']}
            assert [retained[c['url']] for c in original['captures']] == original['captures']
            assert result['transport']['requests'] == before['transport']['requests'] + len(calls)
            assert result['indexing'] == before['indexing']
            assert calls == [('scope_list', 'http://counter.example/i.gif'), ('capture_file', row['scope'] + 'bad.png')] + (
                [('capture_file', 'http://counter.example/i.gif')] if recovered else [])
            assert result['capture_retry'] == ({'files': 0, 'lookups': 0} if recovered else {'files': 1, 'lookups': 1})
            with connect(root) as store:
                saved = unpack(store.db.execute('SELECT * FROM batches WHERE id=?', (before['batch_id'],)).fetchone())
                assert saved['manifest'] == before and saved['state'] == 'capture_continued'
                assert not store.db.execute("SELECT 1 FROM operations WHERE kind='publish'").fetchone()
        assert len(result['captures']) == 4
        assert result['notes'] == [note for note in original['notes'] if note.get('kind') == 'excluded']
        assert result['capture_coverage'][row['id']]['excluded_urls'] == int(with_exclusion)
        assert result['capture_coverage'][row['id']]['state'] == 'complete'
        assert regenerate(app, result).status_code == 409
    finally:
        worker.lease.close()


def test_regeneration_reuses_100_sources_but_inventories_all_files_and_carries_usage(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    row, original = legacy(root, count=100)
    app = create_app(root, start_worker=False)
    assert regenerate(app, original).status_code == 202
    assert detail(app, row)['candidate']['stage'] == 'queued'
    assert detail(app, row)['capture_operation'] is None
    assert regenerate(app, original).status_code == 409
    downloads = []
    class Downloader:
        def __init__(self, store, args):
            assert store.get('wayback_transport') == original['transport']
            assert args.max_requests > 500
        def call(self, job):
            if job['op'] == 'scope_list':
                assert job['url'] == row['scope'] and job['match'] == 'prefix'
                assert (job['from'], job['to']) == ('19990101000000', '20061231235959')
                return {'captures': original['captures'] + [
                    {'url': row['scope'] + 'orphan.zip', 'timestamp': '20061231235959', 'mimetype': 'application/zip'}]}
            downloads.append(job)
            Path(job['destination']).write_bytes(b'ZIP')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': 3,
                    'sha256': digest(b'ZIP'), 'content_type': 'application/zip'}
        def close(self): pass
    monkeypatch.setattr('worker.capture_sites', lambda root, batch, sites, progress: capture_sites(root, batch, sites, Downloader, progress))
    worker = Worker(root)
    try:
        worker.capture_queue()
        assert detail(app, row)['candidate']['stage'] == 'queued'
        make_due(root)
        worker.capture_queue()
        active = detail(app, row)
        assert active['candidate']['stage'] == 'capturing' and active['review'] is None
        assert active['capture_operation']['payload']['sites'][0]['capture_policy'] == POLICY
        assert call(app, 'POST', '/api/undo', {'id': row['id'], 'manifest_sha256': row['manifest_sha256']}).status_code == 409
        worker.operation()
    finally:
        worker.lease.close()
    current = detail(app, row)
    assert current['capture_operation']['state'] == 'completed', current['capture_operation'].get('error')
    assert current['candidate']['stage'] == 'indexing'
    manifest = current['review']['manifest']
    assert manifest['capture_policy'] == POLICY and len(manifest['captures']) == 101
    assert manifest['transport'] == original['transport']
    assert len(downloads) == 1 and downloads[0]['url'].endswith('orphan.zip')
    fields = ('url', 'timestamp', 'path', 'sha256')
    assert [[c[k] for k in fields] for c in manifest['captures'][:100]] == [[c[k] for k in fields] for c in original['captures']]
    with connect(root) as store:
        old = unpack(store.db.execute('SELECT * FROM batches WHERE id=?', (original['batch_id'],)).fetchone())
        assert old['state'] == 'capture_continued' and old['manifest'] == original and old['manifest_sha256'] == digest(original)
        assert not store.db.execute("SELECT 1 FROM operations WHERE kind='publish'").fetchone()


def test_retry_from_a_multi_site_capture_keeps_other_review_and_inventory_untouched(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    first, a = legacy(root, count=1, url='http://first.example/')
    second, b = legacy(root, count=1, identifier='b'*32, url='http://second.example/')
    sites = []
    for manifest, counter in [(a, 'http://counter.example/first.gif'), (b, 'http://counter.example/second.gif')]:
        site = {**manifest['sites'][0], 'capture_policy': POLICY}
        source = site['captures'][0]
        body = f'<p>EQ</p><img src="{counter}">'.encode()
        (root / source['path']).write_bytes(body)
        source.update(bytes=len(body), sha256=digest(body))
        sites.append(site)
    calls = []
    failing = True
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            calls.append(job['url'])
            if job['op'] == 'scope_list' and 'counter.example' not in job['url']:
                return {'captures': [{'url': job['url'] + 'bad.png', 'timestamp': '20061231235959'}]
                        if job['url'] == first['scope'] else []}
            if failing: raise CrawlError('Wayback HTTP 403')
            if job['op'] == 'scope_list':
                return {'captures': [{'url': job['url'], 'timestamp': '20061231235959'}]}
            Path(job['destination']).write_bytes(b'GIF')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': 3,
                    'sha256': digest(b'GIF'), 'content_type': 'image/gif'}
        def close(self): pass
    parent = capture_sites(root, a['batch_id'], sites, Downloader)
    with connect(root) as store:
        store.db.execute('DELETE FROM batches WHERE id=?', (b['batch_id'],))
        store.db.execute('UPDATE batches SET manifest=?,manifest_sha256=? WHERE id=?',
                         (json.dumps(parent), digest(parent), parent['batch_id']))
        store.db.execute('UPDATE candidates SET coverage=? WHERE id=?',
                         (json.dumps({'status':'absent_host','capture':{'batch_id':parent['batch_id']}}), second['id']))
        store.db.commit()
    app = create_app(root, start_worker=False)
    before = detail(app, second)['review']
    reviewed = detail(app, first)['review']['manifest']
    assert reviewed['capture_retry'] == {'files':1, 'lookups':1}
    assert before['manifest']['capture_retry'] == {'files':0, 'lookups':1}
    assert {note['candidate_id'] for note in reviewed['notes']} == {first['id']}
    assert any('counter.example/first.gif' == note['url'].removeprefix('http://') for note in reviewed['notes'])
    assert regenerate(app, reviewed).status_code == 202
    monkeypatch.setattr('worker.capture_sites', lambda root, batch, sites, progress: capture_sites(root, batch, sites, Downloader, progress))
    worker = Worker(root)
    try:
        failing=False;calls.clear();make_due(root);worker.capture_queue();worker.operation()
    finally:
        worker.lease.close()
    result=detail(app, first)
    assert result['candidate']['stage']=='indexing',result['capture_operation']
    assert result['review']['manifest']['capture_retry']=={'files':0,'lookups':0}
    assert len(result['review']['manifest']['captures'])==3
    assert all(capture['candidate_id']==first['id'] for capture in result['review']['manifest']['captures'])
    assert calls==['http://counter.example/first.gif','http://first.example/bad.png','http://counter.example/first.gif']
    after=detail(app,second)['review']
    assert after['manifest']==before['manifest'] and after['manifest_sha256']==before['manifest_sha256']
    with connect(root) as store:
        saved=unpack(store.db.execute('SELECT * FROM batches WHERE id=?',(parent['batch_id'],)).fetchone())
        assert saved['manifest']==parent and saved['state']=='capture_group'


def test_small_legacy_complete_capture_can_be_regenerated_and_undone_after_grace(tmp_path):
    root = tmp_path / 'state'
    row, manifest = legacy(root, coverage='complete')
    app = create_app(root, start_worker=False)
    before = detail(app, row)
    assert regenerate(app, manifest).status_code == 202
    assert detail(app, row)['candidate']['stage'] == 'queued'
    payload = {'id': row['id'], 'manifest_sha256': row['manifest_sha256']}
    assert call(app, 'POST', '/api/scope', {**payload, 'mode': 'site'}).status_code == 409
    assert call(app, 'POST', '/api/decisions', [{**payload, 'decision': 'defer'}]).status_code == 409
    make_due(root)
    assert call(app, 'POST', '/api/undo', payload).status_code == 200
    restored = detail(create_app(root, start_worker=False), row)
    assert restored['candidate']['stage'] == 'indexing'
    assert restored['candidate']['decision'] == before['candidate']['decision']
    assert restored['review']['manifest'] == before['review']['manifest']
    assert regenerate(app, manifest).status_code == 202


def test_regeneration_queue_exceeds_ten_and_passes_paused_capture(tmp_path):
    root = tmp_path / 'state'
    rows = [legacy(root, identifier=f'{index:032x}', url=f'http://guild{index}.example/') for index in range(12)]
    app = create_app(root, start_worker=False)
    with connect(root) as store:
        store.db.execute("INSERT INTO operations VALUES (?,'capture','interrupted','{}',NULL,'Wayback unavailable','now','now')", ('f' * 32,))
        store.db.commit()
    for row, manifest in rows:
        assert regenerate(app, manifest).status_code == 202
    listing = call(app, 'GET', '/api/queue?filter=queued').json()
    assert listing['total'] == 12 and len(listing['operations']) == 1
    assert call(app, 'POST', '/api/capture', {'ids': [row['id'] for row, _ in rows[:2]]}).status_code == 409
    worker = Worker(root)
    try:
        make_due(root)
        worker.capture_queue()
        assert call(app, 'GET', '/api/queue?filter=queued').json()['total'] == 11
        with connect(root) as store:
            assert store.db.execute('SELECT state FROM operations WHERE id=?', ('f' * 32,)).fetchone()[0] == 'interrupted'
        worker.capture_queue()
        assert call(app, 'GET', '/api/queue?filter=queued').json()['total'] == 11
        assert call(app, 'GET', '/api/queue?filter=capturing').json()['total'] == 1
    finally:
        worker.lease.close()


def test_incomplete_capture_blocks_single_bulk_and_legacy_publication(tmp_path):
    root = tmp_path / 'state'
    row, manifest = legacy(root)
    app = create_app(root, start_worker=False)
    payload = {'id': manifest['batch_id'], 'manifest_sha256': digest(manifest)}
    assert call(app, 'POST', '/api/site-decision', {**payload, 'decision': 'approve'}).status_code == 409
    assert call(app, 'POST', '/api/publish', payload).status_code == 409
    preview = call(app, 'GET', '/api/queue?filter=review').json()['review_actions']
    assert preview['incomplete_count'] == 1
    assert call(app, 'POST', '/api/review-decisions', {'token': preview['token'], 'decision': 'approve'}).status_code == 409
    assert regenerate(app, manifest).status_code == 202
    assert call(app, 'POST', '/api/review-decisions', {'token': preview['token'], 'decision': 'decline'}).status_code == 409


@pytest.mark.parametrize('change', ['hash', 'source', 'scope', 'approved', 'published', 'indexed', 'full', 'missing_approval'])
def test_stale_or_already_approved_regeneration_is_rejected_without_work(tmp_path, change):
    root = tmp_path / 'state'
    row, manifest = legacy(root)
    app = create_app(root, start_worker=False)
    with connect(root) as store:
        if change == 'hash': manifest['notes'] = ['changed']
        if change == 'source': (root / manifest['captures'][0]['path']).write_bytes(b'tampered')
        if change == 'scope': store.db.execute("UPDATE candidates SET scope='http://guild.example/other/' WHERE id=?", (row['id'],))
        if change in ('approved', 'published', 'indexed'):
            state = {'approved': 'publication_requested', 'published': 'published_waiting_index', 'indexed': 'indexed'}[change]
            store.db.execute('UPDATE batches SET state=? WHERE id=?', (state, manifest['batch_id']))
        if change == 'full':
            manifest['capture_policy'] = POLICY
            store.db.execute('UPDATE batches SET manifest=?,manifest_sha256=? WHERE id=?', (json.dumps(manifest), digest(manifest), manifest['batch_id']))
        if change == 'missing_approval': store.db.execute('UPDATE candidates SET decision=NULL WHERE id=?', (row['id'],))
        store.db.commit()
    assert regenerate(app, manifest).status_code == 409
    with connect(root) as store:
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()
        assert not store.db.execute("SELECT 1 FROM events WHERE action='continue_capture'").fetchone()


def test_regeneration_rechecks_after_source_preflight_and_rejects_retained_changes(tmp_path):
    from capture_continuation import retained_manifest
    root = tmp_path / 'state'
    row, manifest = legacy(root)
    app = create_app(root, start_worker=False)
    def intervening_decision(root, source):
        check_manifest(root, source)
        with connect(root) as store:
            store.db.execute("UPDATE batches SET state='indexing_declined' WHERE id=?", (manifest['batch_id'],))
            store.db.commit()
    with patch('capture_continuation.check_manifest', intervening_decision):
        assert regenerate(app, manifest).status_code == 409
    with connect(root) as store:
        store.db.execute("UPDATE batches SET state='awaiting_review' WHERE id=?", (manifest['batch_id'],))
        store.db.commit()
    assert regenerate(app, manifest).status_code == 202
    site = {**manifest['sites'][0], 'capture_policy': POLICY,
            'continued_from': {'review_id': manifest['batch_id'], 'manifest_sha256': digest(manifest)}}
    assert retained_manifest(root, site) == manifest
    with pytest.raises(CrawlError, match='changed'):
        retained_manifest(root, {**site, 'scope': 'http://elsewhere.example/'})


def test_old_mixed_batch_regeneration_keeps_other_site_review_and_parent_immutable(tmp_path):
    root = tmp_path / 'state'
    first, a = legacy(root)
    second, b = legacy(root, identifier='b' * 32, url='http://second.example/')
    parent = {**a, 'sites': a['sites'] + b['sites'], 'captures': a['captures'] + b['captures']}
    with connect(root) as store:
        store.db.execute('DELETE FROM batches WHERE id=?', (b['batch_id'],))
        store.db.execute('UPDATE batches SET manifest=?,manifest_sha256=? WHERE id=?', (json.dumps(parent), digest(parent), a['batch_id']))
        store.db.commit()
    app = create_app(root, start_worker=False)
    before = detail(app, second)
    first_review = detail(app, first)['review']
    assert regenerate(app, first_review['manifest']).status_code == 202
    assert detail(app, first)['candidate']['stage'] == 'queued'
    after = detail(create_app(root, start_worker=False), second)
    assert after['candidate']['stage'] == 'indexing' and after['review']['manifest'] == before['review']['manifest']
    assert after['candidate']['coverage']['capture']['batch_id'] == parent['batch_id']
    with connect(root) as store:
        saved = unpack(store.db.execute('SELECT * FROM batches WHERE id=?', (parent['batch_id'],)).fetchone())
        assert saved['state'] == 'capture_group' and saved['manifest'] == parent


def test_regeneration_undo_and_claim_have_one_atomic_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    root = tmp_path / 'state'
    row, manifest = legacy(root)
    app = create_app(root, start_worker=False)
    assert regenerate(app, manifest).status_code == 202
    make_due(root)
    worker = Worker(root)
    barrier = Barrier(2)
    def undo():
        barrier.wait()
        return call(app, 'POST', '/api/undo', {'id': row['id'], 'manifest_sha256': row['manifest_sha256']}).status_code
    def claim():
        barrier.wait()
        worker.capture_queue()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            undone, claimed = pool.submit(undo), pool.submit(claim)
            code = undone.result()
            claimed.result()
        current = detail(app, row)
        assert (code, current['candidate']['stage']) in [(200, 'indexing'), (409, 'capturing')]
    finally:
        worker.lease.close()


def test_queue_positions_follow_regeneration_order_not_old_approval_dates(tmp_path):
    root = tmp_path / 'state'
    first, a = legacy(root)
    second, b = legacy(root, identifier='b' * 32, url='http://second.example/')
    app = create_app(root, start_worker=False)
    assert regenerate(app, b).status_code == 202
    assert regenerate(app, a).status_code == 202
    for view in ('queued', 'approved'):
        rows = call(app, 'GET', '/api/queue?filter=' + view).json()['candidates']
        assert [row['id'] for row in rows] == [second['id'], first['id']]
    assert detail(app, second)['queue_position'] == 1
    assert detail(app, first)['queue_position'] == 2
    worker = Worker(root)
    try:
        with patch('worker.now', return_value='2030-01-01T00:00:00+00:00'):
            worker.capture_queue()
        assert detail(app, second)['candidate']['stage'] == 'capturing'
        assert detail(app, first)['candidate']['stage'] == 'queued'
    finally:
        worker.lease.close()


@pytest.mark.parametrize('payload', [{}, [], {'id': 'bad', 'manifest_sha256': 'stale'}, {'id': 1, 'manifest_sha256': 'stale'}])
def test_regeneration_rejects_invalid_requests(tmp_path, payload):
    assert call(create_app(tmp_path / 'state', start_worker=False), 'POST', '/api/continue-capture', payload).status_code == 409
