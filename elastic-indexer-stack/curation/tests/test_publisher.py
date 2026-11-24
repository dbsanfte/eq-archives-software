import json
import subprocess
from unittest.mock import Mock

import pytest

from common import CrawlError, digest
from publisher import Publisher, cleanup_preparations, git_failure, prepare
from conftest import add_candidate, manifest_for


def test_many_small_sources_use_bounded_git_batches_and_keep_exact_bytes(candidate, tmp_path):
    from captures import archive_path
    root, row = candidate
    publisher = Publisher(root, remote='file://' + str(tmp_path / 'unused-remote'))
    real_git = publisher.git
    real_git('config', 'core.autocrlf', 'true')
    publisher.git = Mock(wraps=real_git)
    captures = []
    try:
        for index in range(515):
            path = root / ('source ' + str(index) + ' "quoted" \\ 日本語\n\t\x01.txt')
            raw = b'Exact source bytes\r\n' + str(index).encode() + (b'\0' if index % 2 else b'\r\n')
            path.write_bytes(raw)
            capture = {**row['captures'][0], 'path': path.name, 'bytes': len(raw), 'sha256': digest(raw),
                       'url': 'http://guild.example/eq/page-' + str(index)}
            capture['archive_path'] = archive_path(capture)
            captures.append(capture)
        changes = publisher.hash_sources(captures)
        calls = publisher.git.call_args_list
        assert len(calls) == 2
        assert all(call.args == ('hash-object', '-w', '--stdin-paths', '--no-filters') for call in calls)
        assert max(len(call.kwargs['data'].splitlines()) for call in calls) == 512
        assert len(changes) == len(captures)
        for capture in (captures[0], captures[1], captures[-1]):
            stored = real_git('cat-file', 'blob', changes[capture['archive_path']], local=True)
            assert stored == (root / capture['path']).read_bytes()
        assert not (publisher.repo / 'index').exists()
    finally:
        publisher.close()


@pytest.mark.parametrize('output', [b'', b'not-an-oid\n', b'1'*40+b'\n'+b'2'*40+b'\n'])
def test_partial_or_malformed_git_batch_cannot_become_a_publication(candidate, output):
    from captures import archive_path
    root, row = candidate
    capture = {**row['captures'][0], 'archive_path': archive_path(row['captures'][0])}
    publisher = object.__new__(Publisher)
    publisher.root = root
    publisher.git = Mock(return_value=output)
    with pytest.raises(CrawlError, match='blob batch'):
        publisher.hash_sources([capture])


def git(path,*args):
    return subprocess.check_output(['git','-C',str(path),*args],stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def archive_server(tmp_path):
    repository = tmp_path / 'archive.git'
    repository.mkdir()
    git(repository, 'init', '-q', '-b', 'master')
    git(repository, 'config', 'user.email', 'fixture@example.org')
    git(repository, 'config', 'user.name', 'Fixture')
    (repository/'unrelated.txt').write_text('untouched')
    git(repository, 'add', 'unrelated.txt')
    git(repository, 'commit', '-qm', 'Existing archive')
    git(repository, 'config', 'receive.denyCurrentBranch', 'ignore')
    git(repository, 'config', 'uploadpack.allowFilter', 'true')
    git(repository, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
    return repository


@pytest.mark.parametrize('delivery', ['confirmed', 'response_lost', 'conflict'])
def test_shared_push_preserves_site_commits_and_marks_only_successful_sites(candidate, archive_server, delivery):
    root, row = candidate
    first = manifest_for(row)
    second = manifest_for(add_candidate(root, 'http://second.example/'), 'b'*32)
    if delivery == 'conflict':
        destination = archive_server/first['captures'][0]['archive_path']
        destination.parent.mkdir(parents=True)
        destination.write_text('other content')
        git(archive_server, 'add', 'websites')
        git(archive_server, 'commit', '-qm', 'Preserve previously archived content')
    before = git(archive_server, 'rev-list', '--count', 'master')
    sites = [prepare(root, manifest, digest(manifest)) for manifest in (first, second)]
    publisher = Publisher(root, remote='file://' + str(archive_server))
    commands = []
    real_git = publisher.git

    def traced_git(*args, **kwargs):
        commands.append(args)
        result = real_git(*args, **kwargs)
        return None if args[0] == 'push' and delivery == 'response_lost' else result

    publisher.git = traced_git
    try:
        results = publisher.publish_many(sites)
        assert sum(args[0] == 'push' for args in commands) == 1
        assert not any(args[:3] == ('hash-object', '-w', '-t') for args in commands)
        site_commits = git(archive_server, 'log', '--format=%H', 'master', '-2').splitlines()
        for site, commit in zip(reversed(sites), site_commits):
            result = results[site.identifier]
            if delivery == 'conflict' and site.identifier == first['batch_id']:
                assert isinstance(result, CrawlError)
                assert destination.read_text() == 'other content'
                continue
            assert result['recovered'] == (delivery == 'response_lost')
            if delivery != 'response_lost':
                assert result['commit'] == commit
            marker = json.loads(git(archive_server, 'show', f'{commit}:{site.marker}'))
            assert marker['manifest_sha256'] == site.expected
            capture = site.manifest['captures'][0]
            assert publisher.git('cat-file', 'blob', git(archive_server, 'rev-parse', f'{commit}:{capture["archive_path"]}'),
                                 local=True) == (root/capture['path']).read_bytes()
        assert git(archive_server, 'rev-list', '--count', 'master') == str(int(before)+(1 if delivery == 'conflict' else 2))
        git(archive_server, 'fsck', '--full')
    finally:
        publisher.close()
        for site in sites:
            site.close()


def test_git_bytes_must_match_the_verified_source_even_if_length_is_unchanged(candidate, tmp_path):
    root, row = candidate
    captures = manifest_for(row)['captures']
    publisher = Publisher(root, remote='file://' + str(tmp_path/'unused.git'))
    real_git = publisher.git

    def change_before_hash(*args, **kwargs):
        capture = captures[0]
        (root/capture['path']).write_bytes(b'?' * capture['bytes'])
        return real_git(*args, **kwargs)

    publisher.git = change_before_hash
    try:
        with pytest.raises(CrawlError, match='blob batch'):
            publisher.hash_sources(captures)
    finally:
        publisher.close()


def test_only_disposable_preparation_directories_are_cleaned(tmp_path):
    import tempfile
    from publisher import PREPARATION, PREPARATION_ROOT
    directory = tmp_path/PREPARATION_ROOT
    directory.mkdir()
    scratch = tempfile.mkdtemp(prefix=PREPARATION, dir=directory)
    original = tmp_path/(PREPARATION+'captures')
    original.mkdir()
    link = directory/(PREPARATION+'wwwwwwww')
    link.symlink_to(original, target_is_directory=True)
    cleanup_preparations(tmp_path)
    from pathlib import Path
    assert not Path(scratch).exists() and original.is_dir() and link.is_symlink()


@pytest.mark.parametrize('stderr,hint',[
    (b'No user exists for uid 10001', 'Unix account'),
    (b'Permission denied (publickey)', 'SSH credential'),
    (b'Host key verification failed', 'SSH host identity'),
    (b'Could not resolve hostname github.com', 'resolve GitHub'),
    (b'No space left on device', 'disk space'),
    (b'Connection timed out', 'timed out'),
    (b'unknown upstream error', 'omitted'),
])
def test_publication_git_errors_identify_stage_without_leaking_upstream_output(stderr,hint):
    error=git_failure(('-c','fetch.negotiationAlgorithm=noop','fetch','origin','private-object'),stderr+b'\nprivate-fixture-secret')
    assert 'Archive Git fetch failed' in error and hint in error
    assert 'private-fixture-secret' not in error and 'private-object' not in error


def test_git_command_uses_safe_stage_error_on_failure(tmp_path,monkeypatch):
    publisher=object.__new__(Publisher)
    publisher.repo=tmp_path/'publisher.git'
    publisher.reader=tmp_path/'reader.git'
    publisher.env={}
    monkeypatch.setattr('publisher.run_git',lambda *args,**kwargs:subprocess.CompletedProcess(args[0],128,b'',b'No user exists for uid 10001\nprivate-fixture-secret'))
    with pytest.raises(CrawlError,match='Archive Git fetch failed.*Unix account') as caught:
        publisher.git('fetch','origin','master')
    assert 'private-fixture-secret' not in str(caught.value)
    assert publisher.git('fetch','origin','master',optional=True) is None


@pytest.mark.parametrize('layout', ['normal', 'embedded_url', 'legacy_embedded_url', 'existing_executable', 'oversized'])
def test_one_batch_commit_keeps_unrelated_files_and_recovers_repeat_publication(candidate,tmp_path,layout,monkeypatch):
    root,row=candidate
    if layout in ('embedded_url', 'legacy_embedded_url'):
        row['captures'][0]['url'] = 'http://guild.example/eq/news.php?next=http://guild.example/guide.html'
    repository=tmp_path/'remote-source'
    repository.mkdir()
    git(repository,'init','-q','-b','master')
    git(repository,'config','user.email','fixture@example.org')
    git(repository,'config','user.name','Fixture')
    (repository/'keep.txt').write_text('unchanged')
    (repository/'a').mkdir()
    (repository/'a/z.txt').write_text('unchanged nested subtree')
    (repository/'a.txt').write_text('Git tree sorting puts a.txt before a/')
    git(repository,'add','keep.txt','a','a.txt')
    if layout == 'existing_executable':
        from captures import archive_path
        destination = repository / archive_path(row['captures'][0])
        destination.parent.mkdir(parents=True)
        destination.write_bytes((root / row['captures'][0]['path']).read_bytes())
        destination.chmod(0o755)
        git(repository, 'add', 'websites')
    git(repository,'commit','-qm','Existing archive')
    unrelated_blob=git(repository,'rev-parse','HEAD:keep.txt')
    unrelated_tree=git(repository,'rev-parse','HEAD:a')
    remote=tmp_path/'archive.git'
    subprocess.run(['git','clone','-q','--bare',str(repository),str(remote)],check=True)
    git(remote,'config','uploadpack.allowFilter','true')
    git(remote,'config','uploadpack.allowAnySHA1InWant','true')
    publisher=Publisher(root,remote='file://'+str(remote))
    real_git=publisher.git
    pushes=[]
    def uncertain_first_push(*args,**kwargs):
        if args[0]=='push':
            pushes.append(args)
            if len(pushes)==1:
                return None  # A connection failed before the remote changed.
        return real_git(*args,**kwargs)
    publisher.git=uncertain_first_push
    try:
        manifest=manifest_for(row)
        if layout == 'legacy_embedded_url':
            from archive_layout import legacy_archive_path
            manifest['captures'][0]['archive_path'] = legacy_archive_path(manifest['captures'][0])
        if layout == 'oversized':
            from captures import archive_path
            first = manifest['captures'][0]
            monkeypatch.setattr('publication_files.GITHUB_BLOB_LIMIT', first['bytes'])
            oversized = b'fixture-only private data' * 40
            (root / 'large.txt').write_bytes(oversized)
            second = {**first, 'url': row['scope'] + 'large.txt', 'path': 'large.txt',
                      'bytes': len(oversized), 'sha256': digest(oversized)}
            second['archive_path'] = archive_path(second)
            manifest['captures'].append(second)
        expected=digest(manifest)
        result=publisher.publish(manifest,expected)
        assert len(pushes)==2 and result['recovered'] is False
        assert git(remote,'rev-list','--count','master') == '2'
        assert git(remote,'show','master:keep.txt') == 'unchanged'
        from pathlib import PurePosixPath
        stored_path = str(PurePosixPath(manifest['captures'][0]['archive_path']))
        assert git(remote,'show','master:'+stored_path).encode() == (root/row['captures'][0]['path']).read_bytes()
        if layout == 'existing_executable':
            assert git(remote, 'ls-tree', 'master', stored_path).startswith('100755 blob ')
        marker = json.loads(git(remote,'show','master:crawl-manifests/'+manifest['batch_id']+'.json'))
        assert marker['manifest_sha256'] == expected
        if layout == 'oversized':
            from publication_files import indexed_captures
            assert [item['archive_path'] for item in marker['captures']] == [stored_path]
            assert result['retained_private'] == marker['retained_private'] == [
                {key: second[key] for key in ('url', 'timestamp', 'bytes', 'sha256', 'archive_path')}]
            assert indexed_captures({'manifest': manifest, 'publication': result}) == [first]
            with pytest.raises(CrawlError, match='exclusions do not match'):
                indexed_captures({'manifest': manifest, 'publication': {**result, 'retained_private': []}})
            assert not git(remote, 'ls-tree', 'master', second['archive_path'])
            assert (root / 'large.txt').read_bytes() == oversized
        repeated=publisher.publish(manifest,expected)
        assert repeated['recovered'] is True
        assert repeated['commit'] == result['commit']
        assert repeated.get('retained_private') == result.get('retained_private')
        assert git(remote,'rev-list','--count','master') == '2'
        assert not (publisher.repo/'index').exists()
        # Verify local packs directly: cat-file would lazily fetch a missing
        # promisor blob and invalidate the I/O assertion.
        inventory=''.join(subprocess.check_output(['git','verify-pack','-v',str(index)]).decode()
                          for index in (publisher.repo/'objects/pack').glob('*.idx'))
        assert unrelated_blob not in inventory
        assert unrelated_tree not in inventory
        assert git(remote,'rev-parse','master:a') == unrelated_tree
        git(remote,'fsck','--full')
        assert (repository/'keep.txt').read_text() == 'unchanged'
    finally:
        publisher.close()


def test_publisher_rejects_stale_manifest_and_existing_different_bytes(candidate,tmp_path):
    root,row=candidate
    repository=tmp_path/'remote-source'
    repository.mkdir()
    git(repository,'init','-q','-b','master')
    git(repository,'config','user.email','fixture@example.org')
    git(repository,'config','user.name','Fixture')
    manifest=manifest_for(row)
    destination=repository/manifest['captures'][0]['archive_path']
    destination.parent.mkdir(parents=True)
    destination.write_text('Existing different source; never overwrite')
    git(repository,'add','websites')
    git(repository,'commit','-qm','Existing archive')
    remote=tmp_path/'archive.git'
    subprocess.run(['git','clone','-q','--bare',str(repository),str(remote)],check=True)
    git(remote,'config','uploadpack.allowFilter','true')
    git(remote,'config','uploadpack.allowAnySHA1InWant','true')
    publisher=Publisher(root,remote='file://'+str(remote))
    try:
        with pytest.raises(CrawlError,match='changed'):
            publisher.publish(manifest,'stale')
        with pytest.raises(CrawlError,match='different data'):
            publisher.publish(manifest,digest(manifest))
        assert git(remote,'rev-list','--count','master') == '1'
        assert not (publisher.repo/'index').exists()
    finally:
        publisher.close()
