import json
import subprocess

import pytest

from common import CrawlError, digest
from publisher import Publisher, git_failure
from conftest import manifest_for


def git(path,*args):
    return subprocess.check_output(['git','-C',str(path),*args],stderr=subprocess.DEVNULL).decode().strip()


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
    monkeypatch.setattr(subprocess,'run',lambda *args,**kwargs:subprocess.CompletedProcess(args[0],128,b'',b'No user exists for uid 10001\nprivate-fixture-secret'))
    with pytest.raises(CrawlError,match='Archive Git fetch failed.*Unix account') as caught:
        publisher.git('fetch','origin','master')
    assert 'private-fixture-secret' not in str(caught.value)
    assert publisher.git('fetch','origin','master',optional=True) is None


def test_one_batch_commit_keeps_unrelated_files_and_recovers_repeat_publication(candidate,tmp_path):
    root,row=candidate
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
        expected=digest(manifest)
        result=publisher.publish(manifest,expected)
        assert len(pushes)==2 and result['recovered'] is False
        assert git(remote,'rev-list','--count','master') == '2'
        assert git(remote,'show','master:keep.txt') == 'unchanged'
        assert git(remote,'show','master:'+manifest['captures'][0]['archive_path']).encode() == (root/row['captures'][0]['path']).read_bytes()
        assert json.loads(git(remote,'show','master:crawl-manifests/'+manifest['batch_id']+'.json'))['manifest_sha256'] == expected
        repeated=publisher.publish(manifest,expected)
        assert repeated['recovered'] is True
        assert repeated['commit'] == result['commit']
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
