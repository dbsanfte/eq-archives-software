"""Source-bound reindex recovery from immutable publication objects."""
import hashlib
import os
from pathlib import Path
import subprocess
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import reindex_embeddings as job
from indexer import gemma,passages
from indexer.source_files import SourceError
from indexer.reindex_diagnostics import Ledger
from indexer.test.test_reindex_embeddings import fixture,V

ID='websites/any.example/20000101000000/guide?.html'


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,
        env={**os.environ,'GIT_AUTHOR_NAME':'Fixture','GIT_COMMITTER_NAME':'Fixture',
        'GIT_AUTHOR_EMAIL':'fixture@example.invalid','GIT_COMMITTER_EMAIL':'fixture@example.invalid'}).decode().strip()


def publication(tmp_path):
    root=tmp_path/'published';root.mkdir();git(root,'init','-q')
    raw=b'<html><title>Guild guide</title><p>A complete cleric healing guide.</p></html>'
    path=root/ID;path.parent.mkdir(parents=True);path.write_bytes(raw)
    git(root,'add','.');git(root,'commit','-qm','published capture')
    commit=git(root,'rev-parse','HEAD');path.unlink()
    source=dict(id=ID,file_type='text',mime_type='text/html',text_full='Prior reader',
        archive_commit=commit,archive_source_sha256=hashlib.sha256(raw).hexdigest())
    return root,raw,source


def test_new_capture_uses_publication_commit_without_changing_prepared_checkout(tmp_path):
    root,raw,source=publication(tmp_path);prepared=tmp_path/'prepared';prepared.mkdir()
    groups,patch,missing=job.prepare(source,prepared,root/'.git')
    assert missing==0 and 'A complete cleric healing guide.' in patch['text_full']
    assert patch['_source_decoding']['source_sha256']==hashlib.sha256(raw).hexdigest()
    assert not (prepared/ID).exists() and not (root/ID).exists()
    assert all(plan.passages for _,plan in groups)


@pytest.mark.parametrize('change',['hash','commit','conflicting_hash'])
def test_publication_recovery_refuses_identity_changes_without_silent_source_replacement(tmp_path,change):
    root,_,source=publication(tmp_path)
    if change=='hash':source['archive_source_sha256']='a'*64
    elif change=='conflicting_hash':source['text_passage_provenance']={'decoding':{'source_sha256':'b'*64}}
    else:source['archive_commit']='a'*40
    if change=='commit':
        _,patch,missing=job.prepare(source,tmp_path/'prepared',root/'.git')
        assert missing==1 and 'text_full' not in patch
    else:
        with pytest.raises(SourceError,match='source_integrity'):job.prepare(source,tmp_path/'prepared',root/'.git')


def test_failed_diagnostics_resolve_only_after_acknowledged_verified_publication_repair(tmp_path):
    root,raw,source=publication(tmp_path);ledger=Ledger(tmp_path/'diagnostics.sqlite')
    ledger.record(ID,model=gemma.MODEL,policy='old',category='source_unavailable')
    hit,client,embedder=fixture();hit.update(_id=ID,_source=source,sort=[ID])
    client.search.side_effect=[{'hits':{'hits':[hit]}},{'hits':{'hits':[]}}]
    result=job.reindex(client,'eq-archive',tmp_path/'prepared',ids=[ID],workers=1,
        factory=lambda **_:embedder,ledger=ledger,publication_repo=root/'.git')
    assert result.updated==1 and result.failed==result.unavailable_sources==0
    record=ledger.report(status='all')['records'][0]
    assert record['status']=='resolved' and record['resolution_source_sha256']==hashlib.sha256(raw).hexdigest()
    assert client.update.call_args.kwargs['doc']['embedding_model']==gemma.MODEL
    ledger.close()


def test_newer_model_policy_is_never_downgraded_by_an_older_continuation(tmp_path):
    hit,client,embedder=fixture();hit['_source'].update(embedding_model=gemma.MODEL,text_chunking_version='eq-passages-480-v999',text_extraction_version='newer')
    before=deepcopy(hit)
    assert job.migrate_record(client,'eq-archive',hit,tmp_path,embedder)==(0,0)
    assert hit==before
    client.update.assert_not_called();embedder.get_chunks_and_embeddings.assert_not_called()


def test_empty_explicit_retry_is_never_a_broad_scan():
    with pytest.raises(ValueError,match='exact record IDs'):job.remaining_query([])


@pytest.mark.parametrize('failed,missing,retryable,expected',[(0,0,0,0),(1,0,0,43),(0,1,0,43),(1,0,1,1)])
def test_terminal_content_gaps_fail_once_but_transport_failure_remains_retryable(tmp_path,monkeypatch,failed,missing,retryable,expected):
    manager=MagicMock();manager._index_name='eq-archive'
    monkeypatch.setattr(job,'ElasticsearchManager',lambda **_:manager)
    monkeypatch.setattr(job,'reindex',lambda *a,**k:SimpleNamespace(failed=failed,unavailable_sources=missing,retryable_failed=retryable))
    monkeypatch.setattr('sys.argv',['reindex','--repo',str(tmp_path),'--id',ID,'--preparation-workers','0'])
    assert job.main()==expected


def test_newer_checkpoint_does_not_silently_resolve_an_unverified_older_rejection(tmp_path):
    hit,client,embedder=fixture();identifier=hit['_id']
    hit['_source'].update(embedding_model=gemma.MODEL,text_chunking_version='eq-passages-480-v999')
    ledger=Ledger(tmp_path/'diagnostics.sqlite');ledger.record(identifier,model=gemma.MODEL,policy='old',error=UnicodeError())
    client.search.side_effect=[{'hits':{'hits':[hit]}},{'hits':{'hits':[]}}]
    result=job.reindex(client,'eq-archive',tmp_path,ids=[identifier],workers=1,factory=lambda **_:embedder,ledger=ledger,include_completed=True)
    assert result.updated==result.failed==0 and ledger.has_pending(identifier)
    client.update.assert_not_called();ledger.close()


def test_excluded_source_checkpoint_stays_bound_to_the_reader_that_is_retained(tmp_path):
    hit,client,embedder=fixture();source=hit['_source']
    source.update(mime_type='text/html',text_extraction_version='old',text_full='Existing preserved historical error reader')
    path=tmp_path/source['id'];path.parent.mkdir(parents=True)
    path.write_text('<html><title>System Message: Error</title><h1>Error</h1><p>The post you selected no longer exists.</p></html>')
    job.migrate_record(client,'eq-archive',hit,tmp_path,embedder)
    patch=client.update.call_args.kwargs['doc']
    assert 'text_full' not in patch and patch['text']==[]
    assert patch['text_passage_provenance']['sources'][0]['source_sha256']==passages.digest(source['text_full'])
    assert patch['llm_summary_vector'] is patch['llm_image_text_vector'] is None


def test_repaired_source_title_is_used_in_every_new_embedding_request(tmp_path):
    title='｢冒険の記録｣';raw=('<html><title>'+title+'</title><p>こんにちは。冒険に行きましょう。</p></html>').encode('iso2022_jp_ext')
    source=dict(id=ID,file_type='text',mime_type='text/html',text_full='Previous reader',
        title=title.encode('iso2022_jp_ext').decode('latin1'))
    path=tmp_path/ID;path.parent.mkdir(parents=True);path.write_bytes(raw)
    groups,patch,missing=job.prepare(source,tmp_path)
    assert patch['title']==title and not missing
    assert all(p.title==title and title in p.request for _,plan in groups for p in plan.passages)
