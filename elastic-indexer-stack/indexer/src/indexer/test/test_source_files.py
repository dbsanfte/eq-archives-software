import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from indexer.source_files import Sources,SourceError,relative

ID='websites/guild.example/20000101000000/guide?.html'


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,
        env={**os.environ,'GIT_AUTHOR_NAME':'Fixture','GIT_COMMITTER_NAME':'Fixture',
             'GIT_AUTHOR_EMAIL':'fixture@example.invalid','GIT_COMMITTER_EMAIL':'fixture@example.invalid'}).decode().strip()


def repository(tmp_path):
    root=tmp_path/'archive';root.mkdir();git(root,'init','-q')
    path=root/ID;path.parent.mkdir(parents=True);path.write_bytes(b'Complete original capture')
    sibling=path.with_name('guidex.html');sibling.write_bytes(b'Wildcard lookalike must not be selected')
    git(root,'add','.');git(root,'commit','-qm','synthetic source')
    commit=git(root,'rev-parse','HEAD');oid=git(root,'rev-parse','HEAD:'+ID)
    return root,commit,oid


@pytest.mark.parametrize('identifier',['','/etc/passwd','websites/../secret','websites/./a','websites//a','objects/x','websites/\0a','x'*16385])
def test_only_literal_supported_archive_relative_paths_are_accepted(identifier):
    with pytest.raises(SourceError,match='source_identity'):relative(identifier)


def test_fast_source_reads_are_bounded_and_hash_verified(tmp_path,monkeypatch):
    root,_,_=repository(tmp_path);sources=Sources([root],tmp_path/'publisher.git')
    data=b'Complete original capture';source={'id':ID,'archive_source_sha256':hashlib.sha256(data).hexdigest()}
    assert sources.read(source)==data and sources.reader is None
    with pytest.raises(SourceError,match='source_integrity'):sources.read({**source,'archive_source_sha256':'a'*64})
    with pytest.raises(SourceError,match='source_integrity'):sources.read({**source,'archive_source_sha256':'bad hash'})
    with pytest.raises(SourceError,match='source_integrity'):
        sources.read({**source,'text_passage_provenance':{'decoding':{'source_sha256':'b'*64}}})
    monkeypatch.setattr('indexer.source_files.LIMIT',8)
    with pytest.raises(SourceError,match='source_too_large'):sources.read(source)
    sources.close()


def test_source_symlinks_cannot_escape_the_readonly_checkout(tmp_path):
    root=tmp_path/'archive';(root/Path(ID).parent).mkdir(parents=True)
    (tmp_path/'outside').write_text('Private unrelated file')
    (root/ID).symlink_to(tmp_path/'outside');sources=Sources([root],tmp_path/'publisher.git')
    with pytest.raises(SourceError,match='source_identity'):sources.read({'id':ID})
    sources.close()


def test_git_fallback_uses_exact_literal_paths_without_checkout_or_remote_configuration(tmp_path):
    root,commit,oid=repository(tmp_path);(root/ID).unlink()
    git(root,'remote','add','origin','https://must-not-be-contacted.invalid/archive')
    git(root,'config','remote.origin.promisor','true');git(root,'config','extensions.partialClone','origin')
    sources=Sources([root],tmp_path/'publisher.git')
    try:
        assert sources.read({'id':ID,'archive_commit':commit})==b'Complete original capture'
        assert sources.git('remote')==b''
        assert sources.read({'id':ID})==b'Complete original capture'
        assert not (root/ID).exists()
        assert list(sources.reader.joinpath('objects').iterdir())  # Only empty Git scaffolding.
        with pytest.raises(SourceError,match='source_unavailable'):
            sources.read({'id':ID,'archive_commit':'a'*40})
        with pytest.raises(SourceError,match='source_unavailable'):sources.read({'id':ID+'missing'})
    finally:sources.close()


def test_missing_promised_objects_never_trigger_the_sources_promisor_remote(tmp_path):
    root,commit,oid=repository(tmp_path);(root/ID).unlink()
    object_file=root/'.git/objects'/oid[:2]/oid[2:];object_file.rename(tmp_path/'held-blob')
    sentinel=tmp_path/'remote-was-contacted'
    git(root,'config','remote.origin.url','ext::sh -c touch% '+str(sentinel))
    git(root,'config','protocol.ext.allow','always')
    git(root,'config','remote.origin.promisor','true');git(root,'config','extensions.partialClone','origin')
    sources=Sources([root],tmp_path/'publisher.git')
    try:
        with pytest.raises(SourceError,match='source_unavailable'):sources.read({'id':ID,'archive_commit':commit})
        assert not sentinel.exists() and not object_file.exists()
    finally:sources.close()


def test_publication_objects_resolve_new_imports_without_an_archive_checkout(tmp_path):
    root,commit,_=repository(tmp_path);sources=Sources([],root/'.git')
    try:assert sources.read({'id':ID,'archive_commit':commit})==b'Complete original capture'
    finally:sources.close()
    absent=Sources([tmp_path/'missing'],tmp_path/'no-publisher')
    with pytest.raises(SourceError,match='source_unavailable'):absent.read({'id':ID})
    absent.close()
