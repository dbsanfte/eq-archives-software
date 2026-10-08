import json
import subprocess
from unittest.mock import patch

import pytest

from common import CAPTURE_WINDOW, CrawlError, digest, now
from captures import archive_path
from conftest import manifest_for
from full_capture import POLICY
from index_captures import index_batch, read_batch
from publisher import Publisher
from server import create_app
from state import connect
from test_publisher import git
from test_server import call


def mixed(root, row):
    manifest = manifest_for(row)
    manifest.update(capture_policy=POLICY, capture_window=dict(CAPTURE_WINDOW),
                    capture_coverage={row['id']: {'state': 'complete'}})
    path = root / 'logo.png'
    path.write_bytes(b'\x89PNG\xff\x00' * 200000)  # larger than the old page-only limit
    capture = {'url': row['scope']+'logo.png', 'timestamp': '20061231235959', 'bytes': path.stat().st_size,
               'path': 'logo.png', 'sha256': digest(path.read_bytes()), 'content_type': 'image/png',
               'kind': 'file', 'candidate_id': row['id'], 'source': 'wayback'}
    capture['archive_path'] = archive_path(capture)
    manifest['captures'].append(capture)
    return manifest


def test_binary_download_and_compact_polls_keep_complete_sources_private(candidate):
    root, row = candidate
    manifest = mixed(root, row)
    batch = manifest['batch_id']
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,?,?)",
                         (batch,json.dumps(manifest),digest(manifest),now(),now()))
        store.db.execute("UPDATE candidates SET state='captured_awaiting_review' WHERE id=?", (row['id'],))
        store.db.commit()
    app = create_app(root, start_worker=False)
    full = call(app, 'GET', '/api/queue?filter=review').json()
    compact = call(app, 'GET', '/api/queue?filter=review&compact=1').json()
    assert full['batches'][0]['manifest'] == manifest
    assert 'manifest' not in compact['batches'][0]
    detail = call(app, 'GET', '/api/candidate?id='+row['id']).json()
    assert detail['review']['manifest']['captures'] == manifest['captures']
    cached = call(app, 'GET', '/api/candidate?id='+row['id']+'&known_manifest='+digest(manifest)).json()['review']
    assert cached['manifest_unchanged'] and 'manifest' not in cached
    assert 'manifest' in call(app, 'GET', '/api/candidate?id='+row['id']+'&known_manifest=stale').json()['review']
    endpoint = '/api/source?batch='+batch+'&slot=1'
    source = call(app, 'GET', endpoint).json()
    assert source['complete_extracted_text'] is None and 'preserved' in source['file_message']
    download = call(app, 'GET', endpoint+'&download=1')
    assert download.content == (root/'logo.png').read_bytes()
    assert download.headers['content-type'] == 'application/octet-stream'
    assert download.headers['content-disposition'].startswith('attachment;')
    assert download.headers['cache-control'] == 'no-store'
    assert call(app, 'GET', endpoint+'&download=1', peer='203.0.113.10').status_code == 403
    (root/'logo.png').write_bytes(b'tampered')
    assert call(app, 'GET', endpoint+'&download=1').status_code == 409


def test_indexing_preserves_assets_without_sending_them_to_es_or_luna(candidate):
    root, row = candidate
    manifest = mixed(root, row)
    class Services:
        def exists(self, identifier):
            assert identifier.endswith('/news.html')
            return True
        def enrich(self, *args): raise AssertionError('No charge for an existing page or a binary file')
        def embedding(self, *args): raise AssertionError('No vectors for a binary file')
    assert index_batch(root, {'manifest': manifest}, Services()) == {'created': 0, 'existing': 1, 'captures': 2, 'preserved_files': 1}
    # Real site manifests can exceed the former 2 MiB sample-batch bound.
    manifest['notes'] = [{'note': 'x' * (2 * 1024**2)}]
    batch = {'manifest': manifest, 'manifest_sha256': digest(manifest), 'publication': {'commit': 'a' * 40}}
    (root/'approved.json').write_text(json.dumps(batch))
    assert read_batch(root, 'approved.json', digest(manifest)) == batch


def test_publisher_streams_large_binary_with_one_commit_and_no_archive_walk(candidate, tmp_path):
    root, row = candidate
    manifest = mixed(root, row)
    repository = tmp_path / 'remote-source'
    repository.mkdir()
    git(repository, 'init', '-q', '-b', 'master')
    git(repository, 'config', 'user.email', 'fixture@example.org')
    git(repository, 'config', 'user.name', 'Fixture')
    (repository/'keep.txt').write_text('untouched')
    git(repository, 'add', 'keep.txt'); git(repository, 'commit', '-qm', 'Existing archive')
    remote = tmp_path/'archive.git'
    subprocess.run(['git', 'clone', '-q', '--bare', str(repository), str(remote)], check=True)
    git(remote, 'config', 'uploadpack.allowFilter', 'true')
    git(remote, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
    publisher = Publisher(root, remote='file://'+str(remote))
    original = publisher.git
    def stream_only(*args, **kwargs):
        if args[0] == 'hash-object' and '-t' not in args and '--stdin' in args:
            assert len(kwargs.get('data', b'')) < 10000  # metadata only; no source bodies
        return original(*args, **kwargs)
    try:
        with patch.object(publisher, 'git', side_effect=stream_only):
            publisher.publish(manifest, digest(manifest))
        assert git(remote, 'rev-list', '--count', 'master') == '2'
        assert subprocess.check_output(['git', '-C', str(remote), 'show', 'master:'+manifest['captures'][1]['archive_path']]) == (root/'logo.png').read_bytes()
        assert git(remote, 'show', 'master:keep.txt') == 'untouched'
    finally:
        publisher.close()


def test_large_review_validation_allows_polling_and_rejects_concurrent_decision(candidate, monkeypatch):
    import asyncio
    import threading
    import httpx
    from captures import check_manifest
    root, row = candidate
    manifest = mixed(root, row)
    batch = manifest['batch_id']
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,?,?)",
                         (batch,json.dumps(manifest),digest(manifest),now(),now()))
        store.db.execute("UPDATE candidates SET state='captured_awaiting_review' WHERE id=?", (row['id'],))
        store.db.commit()
    app = create_app(root, start_worker=False)
    entered, release = threading.Event(), threading.Event()
    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return check_manifest(*args)
    monkeypatch.setattr('server.check_manifest', delayed)
    async def run():
        transport = httpx.ASGITransport(app=app, client=('192.168.50.20',54321))
        async with httpx.AsyncClient(transport=transport, base_url='http://192.168.50.100:8090') as client:
            headers = {'Origin':'http://192.168.50.100:8090','X-Curation-Request':'1'}
            payload = {'id':batch,'manifest_sha256':digest(manifest),'decision':'approve'}
            pending = asyncio.create_task(client.post('/api/site-decision', json=payload, headers=headers))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                response = await asyncio.wait_for(client.get('/api/queue?compact=1&filter=review'), 1)
                assert response.status_code == 200
                declined = await asyncio.wait_for(client.post('/api/site-decision', json={**payload,'decision':'decline'}, headers=headers), 1)
                assert declined.status_code == 200
            finally:
                release.set()
            approval = await pending
            assert approval.status_code == 409  # never revive the intervening decline
            with connect(root) as store:
                assert not store.db.execute("SELECT 1 FROM operations WHERE kind='publish'").fetchone()
    asyncio.run(run())
