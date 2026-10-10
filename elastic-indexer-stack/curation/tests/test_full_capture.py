import json
from pathlib import Path

import pytest

from common import CrawlError, digest
from captures import capture_sites, check_manifest
from conftest import manifest_for
from full_capture import allowed

POLICY = 'complete-files-v1'


def test_full_inventory_keeps_thousands_of_versions_and_every_file_type(candidate):
    root, row = candidate
    site = manifest_for(row)['sites'][0]
    site['capture_policy'] = POLICY
    calls = []
    entries = [{'url': site['scope'] + f'orphan/{index}.{extension}', 'timestamp': stamp,
                'digest': 'SAME', 'length': '30', 'mimetype': mime}
               for index in range(220)
               for stamp in ('19990101000000', '20011231235959', '20061231235959')
               for extension, mime in [('html', 'text/html'), ('png', 'image/png'), ('css', 'text/css'),
                                       ('js', 'application/javascript'), ('zip', 'application/zip')]]
    # Same digest never collapses different URLs or dated versions.
    class Downloader:
        def __init__(self, store, args): calls.append(('open', args.delay, args.bytes_per_second))
        def call(self, job):
            calls.append((job['op'], job['url']))
            if job['op'] == 'scope_list':
                offset = int(job.get('resume_key') or 0)
                return {'captures': entries[offset:offset + 200],
                        'resume_key': str(offset + 200) if offset + 200 < len(entries) else None}
            assert job['op'] == 'capture_file'
            data = b'<p>EverQuest</p>' if job['url'].endswith('.html') else b'\x00\xff\x89asset'
            Path(job['destination']).write_bytes(data)
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': len(data),
                    'sha256': digest(data), 'content_type': next(r['mimetype'] for r in entries if r['url'] == job['url'])}
        def close(self): calls.append(('close',))
    result = capture_sites(root, 'a' * 32, [site], Downloader)
    assert len(result['captures']) == 3301
    assert {c['timestamp'] for c in result['captures']} >= {'19990101000000', '20061231235959'}
    assert sum(c['kind'] == 'file' for c in result['captures']) == 2640
    assert result['capture_coverage'][site['id']]['state'] == 'complete'
    assert calls.count(('open', 0, 0)) == 1
    check_manifest(root, result)
    # Completion and retry are metadata-based; no redownload of any file.
    def unexpected(*args): raise AssertionError('Completed capture reopened transport')
    assert capture_sites(root, 'a' * 32, [site], unexpected) == result


def test_full_capture_pause_keeps_checkpoint_and_never_returns_partial_review(candidate):
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    calls = []
    failing = True
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            calls.append(job['op'])
            if job['op'] == 'scope_list':
                return {'captures': [{'url': site['scope'] + name, 'timestamp': '20061231235959', 'digest': 'D', 'length': '3', 'mimetype': 'image/png'}
                                     for name in ['one.png', 'two.png']], 'resume_key': None}
            if failing and job['url'].endswith('two.png'):
                raise CrawlError('Wayback request budget reached')
            Path(job['destination']).write_bytes(b'png')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'sha256': digest(b'png'), 'bytes': 3, 'content_type': 'image/png'}
        def close(self): pass
    reports=[]
    with pytest.raises(CrawlError, match='budget'):
        capture_sites(root, 'b' * 32, [site], Downloader,progress=reports.append)
    assert (reports[-1]['files'],reports[-1]['versions_found'],reports[-1]['versions_pending'])==(2,3,1)
    assert reports[0]['catalogs_total'] == 1 and reports[0]['catalogs_completed'] == 0
    assert reports[-1]['catalogs_completed'] == 1 and reports[-1]['catalogs_pending'] == 0
    assert not (root / 'batches' / ('b' * 32) / 'manifest.json').exists()
    # Resume the schema used by captures already running before gap tracking.
    import sqlite3
    with sqlite3.connect(root / 'batches' / ('b' * 32) / 'complete' / 'crawl.sqlite3') as checkpoint:
        checkpoint.execute('ALTER TABLE full_queries DROP COLUMN note')
    failing = False
    result = capture_sites(root, 'b' * 32, [site], Downloader,progress=reports.append)
    assert (reports[-1]['files'],reports[-1]['versions_found'],reports[-1]['versions_pending'])==(3,3,0)
    assert reports[-1]['catalogs_total'] == reports[-1]['catalogs_completed'] == 1
    assert len(result['captures']) == 3
    assert calls.count('scope_list') == 1
    assert calls.count('capture_file') == 3  # one success, one failed request, one resumed success


@pytest.mark.parametrize('mode,url,expected', [
    ('directory', 'https://www.guild.example/eq/orphan.zip', True),
    ('directory', 'http://guild.example/eq2/other.html', False),
    ('directory', 'http://other.example/eq/a.png', False),
    ('directory', 'http://guild.example:8080/eq/a.png', False),
    ('directory', 'http://guild.example:443/eq/a.png', False),
    ('directory', 'javascript:bad', False),
    ('page', 'http://guild.example/eq/news.html', True),
    ('page', 'http://guild.example/eq/another.html', False),
    ('ezboard', 'http://guild.example/eq/news.html', False),
])
def test_complete_scope_retains_account_path_and_exact_page_boundaries(candidate, mode, url, expected):
    site = {**manifest_for(candidate[1])['sites'][0], 'scope_mode': mode}
    assert allowed(url, site) == expected
    site.update(scope='http://www.geocities.com/cleric/', scope_mode='site')
    assert not allowed('http://geocities.com/neighbour/image.png', site)


@pytest.mark.parametrize('url', ['http://geocities.com/', 'http://www.sitepowerup.com/mb/'])
def test_unidentified_shared_host_never_runs_a_host_wide_inventory(tmp_path, url):
    from conftest import add_candidate
    root = tmp_path/'state'
    row = add_candidate(root, url=url)
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY, 'scope_mode': 'site', 'scope': url}
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            assert job['op'] == 'scope_list' and job['match'] == 'exact' and job['url'] == url
            return {'captures': []}
        def close(self): pass
    assert len(capture_sites(root, '3'*32, [site], Downloader)['captures']) == 1


def test_initial_plan_and_catalogs_survive_an_interrupted_initialization(candidate, monkeypatch):
    from common import Store
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    saved = Store.set
    def interrupted(self, key, value):
        if key == 'complete_config':
            raise CrawlError('Fixture interrupted plan save')
        return saved(self, key, value)
    with monkeypatch.context() as m:
        m.setattr(Store, 'set', interrupted)
        with pytest.raises(CrawlError, match='plan save'):
            capture_sites(root, '4'*32, [site], None)
    calls = []
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            calls.append(job)
            return {'captures': []}
        def close(self): pass
    capture_sites(root, '4'*32, [site], Downloader)
    assert len(calls) == 1 and calls[0]['op'] == 'scope_list'


def test_external_supporting_files_follow_verified_html_and_css_only(candidate):
    from captures import archive_path
    from common import CAPTURE_WINDOW
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    seed = site['captures'][0]
    raw = b'<p>EQ</p><link rel="stylesheet" href="https://static.example/eq.css"><a href="http://other.example/">Other site</a>'
    (root / seed['path']).write_bytes(raw)
    seed.update(bytes=len(raw), sha256=digest(raw))
    calls = []
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            calls.append(job)
            if job['op'] == 'scope_list':
                assert job['from'] == CAPTURE_WINDOW['from'] and job['to'] == CAPTURE_WINDOW['to']
                if job['url'].startswith(site['scope'].rstrip('/')):
                    return {'captures': [], 'resume_key': None}
                assert job['match'] == 'exact'
                return {'captures': [{'url': job['url'], 'timestamp': '20061231235959', 'digest': 'A', 'length': '20',
                                      'mimetype': 'text/css' if job['url'].endswith('.css') else 'image/png'}]}
            data = b'body {background: url("sword.png")}' if job['url'].endswith('.css') else b'\x89PNG\x00'
            Path(job['destination']).write_bytes(data)
            return {'url': job['url'], 'timestamp': job['timestamp'], 'sha256': digest(data), 'bytes': len(data),
                    'content_type': 'text/css' if job['url'].endswith('.css') else 'image/png'}
        def close(self): pass
    result = capture_sites(root, 'c' * 32, [site], Downloader)
    assert len(result['captures']) == 3
    assert not any('other.example' in job['url'] for job in calls)
    asset = result['captures'][-1]
    assert asset['supporting_source']['url'] == 'https://static.example/eq.css'
    check_manifest(root, result)
    # A fabricated URL or parent cannot widen the approval, even if its file hash is valid.
    original = dict(asset)
    asset['url'] = 'https://static.example/unrelated.png'
    asset['archive_path'] = archive_path(asset)
    with pytest.raises(CrawlError, match='not referenced'):
        check_manifest(root, result)
    asset.update(original)
    asset['supporting_source'] = {'url': asset['url'], 'timestamp': asset['timestamp'], 'sha256': asset['sha256']}
    with pytest.raises(CrawlError, match='no verified source'):
        check_manifest(root, result)


def test_ad_subdomains_are_skipped_without_losing_normal_assets_or_creating_retries(candidate):
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    seed = site['captures'][0]
    blocked = ['http://ad.network.example/banner.gif', 'https://ADS.network.example:443/banner.js']
    kept = ['https://adventure.example/banner.gif', 'https://cdn.example/ads/banner.gif']
    body = ('<p>EverQuest</p>' + ''.join(f'<img src="{url}">' for url in blocked + kept)).encode()
    (root / seed['path']).write_bytes(body)
    seed.update(bytes=len(body), sha256=digest(body))
    calls, reports = [], []
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            calls.append(job)
            if job['op'] == 'scope_list':
                return {'captures': [] if job['match'] == 'prefix' else
                        [{'url': job['url'], 'timestamp': '20000101000000'}]}
            Path(job['destination']).write_bytes(b'image')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': 5,
                    'sha256': digest(b'image'), 'content_type': 'image/gif'}
        def close(self): pass
    result = capture_sites(root, '5' * 32, [site], Downloader, reports.append)
    assert not any('network.example' in job['url'] for job in calls)
    assert {c['url'] for c in result['captures']} == {seed['url'], *kept}
    assert result['capture_retry'] == {'files': 0, 'lookups': 0}
    assert result['capture_coverage'][site['id']]['state'] == 'complete'
    assert result['capture_coverage'][site['id']]['excluded_urls'] == 2
    assert reports[-1]['excluded_urls'] == 2 and reports[-1]['failed_lookups'] == 0
    assert len(result['notes']) == 2 and all(note['kind'] == 'excluded' for note in result['notes'])
    check_manifest(root, result)


def test_resuming_old_capture_skips_ad_replays_and_failed_lookups_but_keeps_saved_files(candidate, monkeypatch):
    from common import Store
    from source_assets import references
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    seed = site['captures'][0]
    urls = [f'http://ads.network.example/{name}.gif' for name in ('0saved', '1missing', '2blocked', '3pending', '4later')]
    body = ('<p>EverQuest</p>' + ''.join(f'<img src="{url}">' for url in urls)).encode()
    (root / seed['path']).write_bytes(body)
    seed.update(bytes=len(body), sha256=digest(body))
    monkeypatch.setattr('source_assets.references', lambda *args: sorted(references(*args)))
    calls = []
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            calls.append(job)
            if job['op'] == 'scope_list':
                if '2blocked' in job['url']: raise CrawlError('Wayback HTTP 403')
                return {'captures': [] if job['match'] == 'prefix' else
                        [{'url': job['url'], 'timestamp': '20000101000000'}]}
            if '1missing' in job['url']: raise CrawlError('Wayback HTTP 404')
            if '3pending' in job['url']: raise CrawlError('Wayback HTTP 503')
            Path(job['destination']).write_bytes(b'image')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': 5,
                    'sha256': digest(b'image'), 'content_type': 'image/gif'}
        def close(self): pass
    with monkeypatch.context() as old:
        old.setattr('full_capture.capture_exclusion', lambda url: None, raising=False)
        with pytest.raises(CrawlError, match='503'):
            capture_sites(root, '6' * 32, [site], Downloader)
    checkpoint = Store(root / 'batches' / ('6' * 32) / 'complete')
    # Existing checkpoints lack the new exclusion table. Migration must retain
    # both the successful file and its immutable receipt/source verification.
    checkpoint.db.execute('DROP TABLE IF EXISTS full_exclusions')
    saved = json.loads(checkpoint.db.execute("SELECT capture FROM full_records WHERE url=?", (urls[0],)).fetchone()[0])
    checkpoint.db.commit(); checkpoint.close()
    before = len(calls)
    result = capture_sites(root, '6' * 32, [site], Downloader)
    assert len(calls) == before
    # The later good file is now saved before the temporary failure pauses.
    assert saved in result['captures'] and len(result['captures']) == 3
    assert result['capture_retry'] == {'files': 0, 'lookups': 0}
    assert result['capture_coverage'][site['id']]['excluded_urls'] == 3
    assert {note['url'] for note in result['notes']} == set(urls[1:4])
    check_manifest(root, result)


def test_pending_ad_catalog_from_old_checkpoint_is_excluded_before_a_request(candidate, monkeypatch):
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    seed = site['captures'][0]
    body = b'<p>EQ history</p><img src="http://ad.example/banner.gif">'
    (root / seed['path']).write_bytes(body)
    seed.update(bytes=len(body), sha256=digest(body))
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            if job['url'].startswith('http://ad.'):
                raise CrawlError('Wayback HTTP 503')
            return {'captures': []}
        def close(self): pass
    with monkeypatch.context() as old:
        old.setattr('full_capture.capture_exclusion', lambda url: None)
        with pytest.raises(CrawlError, match='503'):
            capture_sites(root, '7' * 32, [site], Downloader)
    def no_transport(*args): raise AssertionError('Excluded catalog reopened transport')
    result = capture_sites(root, '7' * 32, [site], no_transport)
    assert len(result['captures']) == 1
    assert result['capture_retry'] == {'files': 0, 'lookups': 0}
    assert result['capture_coverage'][site['id']]['excluded_urls'] == 1
    check_manifest(root, result)


@pytest.mark.parametrize('failure', ['Wayback HTTP 403', 'Wayback HTTP 404', 'Wayback HTTP 410', 'Replay returned a different dated version; requested version remains unavailable'])
def test_unavailable_versions_remain_explicit_gaps(candidate, failure):
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    progress = []
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            if job['op'] == 'scope_list':
                return {'captures': [{'url': site['scope'] + 'missing.png', 'timestamp': '20061231235959', 'digest': 'D', 'length': '1'}]}
            raise CrawlError(failure)
        def close(self): pass
    result = capture_sites(root, 'd' * 32, [site], Downloader, progress.append)
    assert result['capture_coverage'][site['id']]['state'] == 'complete_with_gaps'
    assert result['notes'][0]['timestamp'] == '20061231235959'
    assert result['notes'][0]['note'] == failure
    assert progress[-1]['phase'] == 'ready_for_review' and progress[-1]['unavailable'] == 1
    check_manifest(root, result)


def test_blocked_external_counter_catalog_does_not_stop_site_files(candidate):
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    source = site['captures'][0]
    body = b'<p>EQ history</p><img src="http://v1.extreme-dm.com/i.gif">'
    (root / source['path']).write_bytes(body)
    source.update(bytes=len(body), sha256=digest(body))
    calls, reports = [], []
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            calls.append(job)
            if job['url'] == 'http://v1.extreme-dm.com/i.gif':
                assert job['op'] == 'scope_list' and job['match'] == 'exact'
                raise CrawlError('Wayback HTTP 403')
            if job['op'] == 'scope_list':
                return {'captures': [{'url': site['scope'] + 'orphan.zip', 'timestamp': '20061231235959'}]}
            Path(job['destination']).write_bytes(b'zip')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'bytes': 3,
                    'sha256': digest(b'zip'), 'content_type': 'application/zip'}
        def close(self): pass
    result = capture_sites(root, '2' * 32, [site], Downloader, reports.append)
    assert len(result['captures']) == 2
    assert result['notes'] == [{'candidate_id': site['id'], 'url': 'http://v1.extreme-dm.com/i.gif',
                               'note': 'Supporting-file lookup failed: Wayback HTTP 403'}]
    assert result['capture_coverage'][site['id']]['state'] == 'complete_with_gaps'
    assert result['capture_retry'] == {'files': 0, 'lookups': 1}
    assert reports[-1]['failed_lookups'] == 1
    assert len(calls) == 3
    check_manifest(root, result)


@pytest.mark.parametrize('operation,failure', [('scope_list','Wayback HTTP 403'),
                                             ('capture_file','Wayback HTTP 429'),
                                             ('capture_file','Wayback HTTP 503')])
def test_scope_catalog_and_service_failures_still_pause(candidate, operation, failure):
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            if job['op'] == operation: raise CrawlError(failure)
            return {'captures': [{'url': site['scope'] + 'one.png', 'timestamp': '20061231235959'}]}
        def close(self): pass
    with pytest.raises(CrawlError, match=failure):
        capture_sites(root, '2' * 32, [site], Downloader)
    assert not (root / 'batches' / ('2' * 32) / 'complete' / 'manifest.json').exists()


def test_partial_catalog_repeated_cursor_wrong_date_and_disk_full_cannot_complete(candidate, monkeypatch):
    from types import SimpleNamespace
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job): return {'captures': [], 'resume_key': 'same'}
        def close(self): pass
    with pytest.raises(CrawlError, match='continuation'):
        capture_sites(root, 'e' * 32, [site], Downloader)
    with pytest.raises(CrawlError, match='changed'):
        capture_sites(root, 'e' * 32, [{**site, 'scope': 'http://guild.example/other/'}], Downloader)
    monkeypatch.setattr('full_capture.shutil.disk_usage', lambda _: SimpleNamespace(free=0))
    with pytest.raises(CrawlError, match='disk'):
        capture_sites(root, 'f' * 32, [site], Downloader)


def test_resource_references_cover_historic_markup_and_css():
    from source_assets import Resources
    parser = Resources('http://guild.example/eq/')
    parser.feed('''<base href="http://static.example/eq/"><body background="tile.gif">
        <img src="a.png" srcset="b.png 2x, c.png 3x"><script src="main.js"></script>
        <link rel="shortcut icon" href="favicon.ico"><object data="map.swf"></object>
        <a href="map.zip">Map</a><a download href="file?id=12">Download</a>
        <a href="http://unrelated.example/">Another site</a><video poster="poster.jpg"></video>
        <style>@import 'more.css'; body {background:url(bg.png)}</style>
        <div style="background:url(inline.jpg)"></div>''')
    assert {u.rsplit('/', 1)[-1] for u in parser.urls} == {
        'tile.gif','a.png','b.png','c.png','main.js','favicon.ico','map.swf','map.zip','file?id=12',
        'poster.jpg','more.css','bg.png','inline.jpg'}


@pytest.mark.parametrize('platform,url', [('ezboard','http://pub4.ezboard.com/beqasylum'),
                                        ('sitepowerup','http://www.sitepowerup.com/mb/view.asp?BoardID=102010')])
def test_new_board_capture_includes_referenced_files_without_crawling_other_boards(tmp_path, platform, url):
    from conftest import add_candidate
    root = tmp_path / 'state'
    body = f'<title>EverQuest board</title><p>EverQuest guild history.</p><a href="{url}">Board index</a><img src="http://static.example/logo.gif">'.encode()
    row = add_candidate(root, url=url, body=body)
    from importlib import import_module
    site = {**manifest_for(row)['sites'][0], 'scope_mode': platform, 'scope': import_module(platform).board_url(url), 'capture_policy': POLICY}
    calls = []
    class Downloader:
        def __init__(self, *args): pass
        def call(self, job):
            calls.append(job)
            if job['op'] == platform + '_list':
                return {'captures': [], 'resume_key': None}
            if job['op'] == 'scope_list':
                assert job['url'] == 'http://static.example/logo.gif' and job['match'] == 'exact'
                return {'captures': [{'url': job['url'], 'timestamp': '20061231235959', 'digest': 'D', 'length': '3', 'mimetype': 'image/gif'}]}
            Path(job['destination']).write_bytes(b'GIF89a')
            return {'url': job['url'], 'timestamp': job['timestamp'], 'sha256': digest(b'GIF89a'), 'bytes': 6, 'content_type': 'image/gif'}
        def close(self): pass
    result = capture_sites(root, '1' * 32, [site], Downloader)
    assert len(result['captures']) == 2
    assert result['capture_policy'] == POLICY and result['capture_coverage'][site['id']]['state'] == 'complete'
    assert result['captures'][-1]['supporting_source']['url'] == url
    assert not any(job['op'] == 'scope_list' and platform in job['url'] for job in calls)
    check_manifest(root, result)


def test_explicit_resume_extends_exhausted_transport_without_resetting_usage(candidate):
    from common import Store
    from full_capture import ALLOWANCE
    root, row = candidate
    site = {**manifest_for(row)['sites'][0], 'capture_policy': POLICY}
    attempts = []
    class Downloader:
        def __init__(self, store, args):
            attempts.append((store.get('wayback_transport', {}), args.max_requests))
            self.store = store
        def call(self, job):
            if len(attempts) == 1:
                self.store.set('wayback_transport', {'requests': ALLOWANCE['requests'], 'bytes': 40, 'seconds': 10})
                raise CrawlError('Wayback request budget reached')
            return {'captures': []}
        def close(self): pass
    with pytest.raises(CrawlError, match='budget'):
        capture_sites(root, '2' * 32, [site], Downloader)
    capture_sites(root, '2' * 32, [site], Downloader)
    assert attempts[1][0]['requests'] == ALLOWANCE['requests']
    assert attempts[1][1] == ALLOWANCE['requests'] * 2
    store = Store(root / 'batches' / ('2' * 32) / 'complete')
    try:
        assert store.get('wayback_transport')['bytes'] == 40
    finally:
        store.close()
