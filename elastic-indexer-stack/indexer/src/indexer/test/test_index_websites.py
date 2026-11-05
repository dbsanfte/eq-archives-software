from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import subprocess
import sys

from elasticsearch import ApiError,ConflictError
import pytest

import index_websites as job
from indexer import gemma,passages
from indexer.html_extraction import WEBSITE_EXTRACTION_VERSION

HOST='guild.example'
V=[1.]+[0.]*767


def git(repo,*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()


def repository(tmp_path,count=2):
    repo=tmp_path/'archive';repo.mkdir();git(repo,'init','-q');git(repo,'config','user.name','Fixture');git(repo,'config','user.email','fixture@example.org')
    keys=[]
    for i in range(count):
        stamp=(datetime(2000,1,1)+timedelta(seconds=i)).strftime('%Y%m%d%H%M%S');key=f'websites/{HOST}/{stamp}/page.html';p=repo/key;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(f'<title>Guild chronicle {i}</title><p>Complete firsthand evidence from adventurer {i}.</p>');keys.append(key)
    (repo/'websites'/HOST/'20000101000000'/'photo.png').write_bytes(b'not website text')
    other=repo/'websites'/'other.example'/'20000101000000'/'page.html';other.parent.mkdir(parents=True);other.write_text('<p>Unselected site</p>')
    git(repo,'add','.');git(repo,'commit','-qm','Immutable sources')
    return repo,keys


class Client:
    def __init__(self):self.docs={}
    def mget(self,*,index,ids,source):
        assert source is False
        return {'docs':[{'_id':key,'found':key in self.docs} for key in ids]}
    def create(self,*,index,id,document):
        assert id not in self.docs
        self.docs[id]=document;return {'result':'created'}


def embedder():
    e=MagicMock()
    e.get_chunks_and_embeddings.side_effect=lambda document,**kw:[{'text_chunk':p.text,'title':p.title,'context':p.context,'kind':p.kind,'source_start':p.start,'source_end':p.end,'vector':V} for p in kw['_prepared_passages'].passages]
    return e


def test_import_resumes_selected_hosts_creates_searchable_readers_and_never_enriches(tmp_path):
    repo,keys=repository(tmp_path);state=tmp_path/'private/state.sqlite';client=Client();e=embedder()
    inv=job.TextInventory(repo,state)
    try:
        p=job.index_websites(client,'eq',inv,e,hosts=[HOST],limit=1)
        assert p.created==1 and len(client.docs)==1
    finally:inv.close()
    inv=job.TextInventory(repo,state)
    try:
        p=job.index_websites(client,'eq',inv,e,hosts=[HOST]);assert p.created==1 and p.existing==0 and p.remaining_gaps==0
        assert not list(inv.pending_hosts([HOST]))
        assert inv.git('remote')==b''
    finally:inv.close()
    assert set(client.docs)==set(keys)
    for key,d in client.docs.items():
        assert d['id']==key and d['file_type']=='text' and d['domain_name']==HOST
        assert d['embedding_model']==gemma.MODEL and d['text_chunking_version']==passages.VERSION
        assert d['text_extraction_version']==WEBSITE_EXTRACTION_VERSION
        assert 'Page URL:' in d['text_full'] and 'Complete firsthand evidence' in d['text_full']
        assert d['text'][0]['vector']==V and 'firsthand evidence' in d['text'][0]['text_chunk']
        assert len(d['archive_source_sha256'])==64 and d['archive_commit']==git(repo,'rev-parse','HEAD')
        assert d['text_passage_provenance']['decoding']['source_sha256']==d['archive_source_sha256']
        assert not any(k.startswith('llm_') for k in d)
    e.call_openai_api_text.assert_not_called();assert git(repo,'status','--porcelain')==''


def test_existing_enriched_records_are_not_overwritten_or_sent_for_embedding(tmp_path):
    repo,keys=repository(tmp_path,count=1);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');client=Client();e=embedder();original={'llm_summary':'Original enrichment','archive_commit':'other','title':'Operator title'};client.docs[keys[0]]=original.copy()
    try:
        p=job.index_websites(client,'eq',inv,e,hosts=[HOST]);assert p.existing==1 and p.created==0
        assert client.docs[keys[0]]==original;e.get_chunks_and_embeddings.assert_not_called()
    finally:inv.close()


def test_limit_at_a_metadata_batch_boundary_does_not_rewind_completed_files(tmp_path):
    repo,keys=repository(tmp_path,count=55);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');client=Client();e=embedder()
    try:
        assert job.index_websites(client,'eq',inv,e,hosts=[HOST],limit=50).created==50
        p=job.index_websites(client,'eq',inv,e,hosts=[HOST],limit=1)
        assert p.created==1 and p.existing==0 and len(client.docs)==51
    finally:inv.close()


@pytest.mark.parametrize('hosts',[[],['absent.example'],['../guild.example'],['guild.example/path']])
def test_missing_or_unsafe_host_scope_is_not_silently_treated_as_completion(tmp_path,hosts):
    repo,_=repository(tmp_path);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');e=embedder()
    try:
        with pytest.raises(ValueError):job.index_websites(Client(),'eq',inv,e,hosts=hosts)
        e.get_chunks_and_embeddings.assert_not_called()
    finally:inv.close()


def test_scope_changes_and_second_writer_are_rejected(tmp_path):
    repo,_=repository(tmp_path);state=tmp_path/'state/x.sqlite';inv=job.TextInventory(repo,state)
    try:
        inv.select_hosts([HOST])
        with pytest.raises(ValueError,match='scope changed'):inv.select_hosts(['other.example'])
        with pytest.raises(ValueError,match='already owns'):job.TextInventory(repo,state)
    finally:inv.close()


def test_bad_source_is_remembered_while_other_files_advance_and_retry_is_explicit(tmp_path,monkeypatch):
    repo,keys=repository(tmp_path);state=tmp_path/'state/x.sqlite';inv=job.TextInventory(repo,state);client=Client();e=embedder();real=job.prepare_document
    def failing(inventory,key,blob):
        if key==keys[0]:raise RecursionError('damaged source')
        return real(inventory,key,blob)
    monkeypatch.setattr(job,'prepare_document',failing)
    try:
        p=job.index_websites(client,'eq',inv,e,hosts=[HOST]);assert p.failed==1 and p.created==1 and p.remaining_gaps==1
        assert list(client.docs)==[keys[1]]
        assert inv.db.execute('SELECT reason FROM gaps').fetchone()[0]=='extraction:RecursionError'
        monkeypatch.setattr(job,'prepare_document',real)
        assert job.index_websites(client,'eq',inv,e,hosts=[HOST]).created==0
        p=job.index_websites(client,'eq',inv,e,hosts=[HOST],retry_failed=True)
        assert p.created==1 and p.remaining_gaps==0 and set(client.docs)==set(keys)
    finally:inv.close()


def test_modified_source_or_symlink_is_never_published_but_later_files_continue(tmp_path):
    repo,keys=repository(tmp_path);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');(repo/keys[0]).write_text('Different from pinned source')
    try:
        p=job.index_websites(Client(),'eq',inv,embedder(),hosts=[HOST]);assert p.failed==1 and p.created==1
        (repo/keys[0]).unlink();(repo/keys[0]).symlink_to(repo/keys[1])
        p=job.index_websites(Client(),'eq',inv,embedder(),hosts=[HOST],retry_failed=True)
        assert p.failed==1 and p.remaining_gaps==1
    finally:inv.close()


def test_missing_worktree_file_reads_only_local_git_blob_without_recreating_the_checkout(tmp_path):
    repo,keys=repository(tmp_path,count=1);(repo/keys[0]).unlink();inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');client=Client()
    try:
        p=job.index_websites(client,'eq',inv,embedder(),hosts=[HOST]);assert p.created==1 and p.unavailable==0
        assert not (repo/keys[0]).exists() and 'firsthand evidence' in client.docs[keys[0]]['text_full']
    finally:inv.close()


def test_unavailable_local_object_and_byte_limit_stay_in_durable_diagnostics(tmp_path,monkeypatch):
    repo,keys=repository(tmp_path,count=1);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');(repo/keys[0]).unlink();real=inv.git
    def missing(*args):
        if args[0]=='cat-file':raise ValueError('Unavailable; do not fetch')
        return real(*args)
    monkeypatch.setattr(inv,'git',missing)
    try:
        p=job.index_websites(Client(),'eq',inv,embedder(),hosts=[HOST]);assert p.unavailable==1 and p.remaining_gaps==1
        monkeypatch.setattr(inv,'git',real);monkeypatch.setattr(job,'MAX_BYTES',1)
        p=job.index_websites(Client(),'eq',inv,embedder(),hosts=[HOST],retry_failed=True);assert p.unsupported==1 and p.remaining_gaps==1
        assert inv.db.execute('SELECT reason FROM gaps').fetchone()[0]=='source_byte_limit'
    finally:inv.close()


@pytest.mark.parametrize('lookup',[
 {'docs':[]},{'docs':[{'_id':'wrong','found':False}]},
 {'docs':[{'_id':None,'error':'failure'}]},
])
def test_incomplete_lookup_cannot_write_or_advance_a_cursor(tmp_path,lookup):
    repo,_=repository(tmp_path,count=1);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');client=MagicMock();client.mget.return_value=lookup
    try:
        with pytest.raises(ValueError,match='lookup'):job.index_websites(client,'eq',inv,embedder(),hosts=[HOST])
        assert next(inv.pending_hosts([HOST]))[2]==0;client.create.assert_not_called()
    finally:inv.close()


@pytest.mark.parametrize('bad', ['missing','nan','dimensions'])
def test_invalid_embedding_response_is_rejected_before_creating_or_marking_completion(tmp_path,bad):
    repo,_=repository(tmp_path,count=1);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');e=embedder();real=e.get_chunks_and_embeddings.side_effect
    def broken(*args,**kwargs):
        chunks=real(*args,**kwargs)
        if bad=='missing':return []
        chunks[0]['vector']=[float('nan')]*768 if bad=='nan' else [1.]*767
        return chunks
    e.get_chunks_and_embeddings.side_effect=broken;client=Client()
    try:
        with pytest.raises(ValueError):job.index_websites(client,'eq',inv,e,hosts=[HOST])
        assert client.docs=={} and next(inv.pending_hosts([HOST]))[2]==0
    finally:inv.close()


def test_indexing_conflict_preserves_the_winning_writer_and_oversized_record_does_not_block_others(tmp_path):
    repo,keys=repository(tmp_path);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');client=Client();real=client.create
    def create(**kw):
        if kw['id']==keys[0]:raise ApiError('oversize',meta=SimpleNamespace(status=413),body={})
        return real(**kw)
    client.create=create
    try:
        p=job.index_websites(client,'eq',inv,embedder(),hosts=[HOST]);assert p.failed==1 and p.created==1
        client.create=MagicMock(side_effect=ConflictError('winner',meta=SimpleNamespace(status=409),body={}))
        p=job.index_websites(client,'eq',inv,embedder(),hosts=[HOST],retry_failed=True)
        assert p.existing==1 and p.remaining_gaps==0
    finally:inv.close()


def test_authentication_error_stops_without_advancing_source_checkpoints(tmp_path):
    repo,_=repository(tmp_path,count=1);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');client=Client();client.create=MagicMock(side_effect=ApiError('auth',meta=SimpleNamespace(status=403),body={}))
    try:
        with pytest.raises(ApiError):job.index_websites(client,'eq',inv,embedder(),hosts=[HOST])
        assert next(inv.pending_hosts([HOST]))[2]==0
    finally:inv.close()


def test_plain_text_xml_invalid_identity_and_nontext_candidates(tmp_path,monkeypatch):
    repo,keys=repository(tmp_path,count=1);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite')
    try:
        blob=git(repo,'rev-parse','HEAD:'+keys[0])
        monkeypatch.setattr(job,'source_bytes',lambda *args:b'Plain first-hand raid account.\nThe entire last line.')
        source,plan=job.prepare_document(inv,keys[0],blob)
        assert source['mime_type']=='text/plain' and 'entire last line' in source['text_full'] and plan.passages
        monkeypatch.setattr(job,'source_bytes',lambda *args:b'\x00\xff\x00\x01')
        with pytest.raises(job.Unsupported,match='unsupported_media'):job.prepare_document(inv,keys[0],blob)
        for key in ['websites/guild.example/no-date/file.html','websites/guild.example/20009999000000/file.html']:
            with pytest.raises(job.Unsupported,match='identity'):job.prepare_document(inv,key,blob)
    finally:inv.close()


def test_main_cli_selects_saved_environment_scope_without_paid_enrichment(tmp_path,monkeypatch):
    repo,keys=repository(tmp_path,count=1);client=Client();manager=MagicMock();manager.get_client.return_value=client;manager._index_name='eq'
    factory=MagicMock(return_value=manager);monkeypatch.setattr(job,'ElasticsearchManager',factory)
    monkeypatch.setattr(job,'OpenAIManager',MagicMock(return_value=embedder()));monkeypatch.setenv('TEXT_IMPORT_HOSTS',HOST)
    monkeypatch.setattr(sys,'argv',['job','--repo',str(repo),'--state',str(tmp_path/'state/x.sqlite')])
    assert job.main()==0 and set(client.docs)==set(keys)
    factory.assert_called_once_with(manage_index=False)
    monkeypatch.setattr(sys,'argv',['job','--limit','-1'])
    with pytest.raises(SystemExit) as error:job.main()
    assert error.value.code==2


def test_operator_host_priority_is_respected_while_saved_scope_identity_remains_stable(tmp_path):
    repo,_=repository(tmp_path,count=1);inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');client=Client()
    try:
        p=job.index_websites(client,'eq',inv,embedder(),hosts=['other.example',HOST],limit=1)
        assert p.created==1 and next(iter(client.docs)).startswith('websites/other.example/')
        p=job.index_websites(client,'eq',inv,embedder(),hosts=[HOST,'other.example'])
        assert p.created==1 and len(client.docs)==2
    finally:inv.close()


def test_backfill_uses_the_shared_japanese_decoder_with_the_legacy_detection_hint(tmp_path,monkeypatch):
    repo,keys=repository(tmp_path,count=1);text='<html><meta charset="utf-8"><title>冒険</title><p>こんにちは①</p></html>'
    (repo/keys[0]).write_bytes(text.encode('cp932'));git(repo,'add','.');git(repo,'commit','-qm','Japanese archived bytes')
    inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');client=Client()
    monkeypatch.setattr(job.chardet,'detect',lambda raw:{'encoding':'Shift_JIS','confidence':.99})
    try:
        assert job.index_websites(client,'eq',inv,embedder(),hosts=[HOST]).created==1
        assert 'こんにちは①' in client.docs[keys[0]]['text_full']
        assert client.docs[keys[0]]['text_passage_provenance']['decoding']['encoding']=='cp932'
    finally:inv.close()


def test_opaque_forum_routes_are_imported_and_dynamic_image_responses_are_typed_exclusions(tmp_path):
    from PIL import Image
    import io
    repo,keys=repository(tmp_path,count=1);folder=repo/keys[0];folder=folder.parent
    opaque=folder/'fclerics.showMessage?topicID=1.topic';opaque.write_text('<title>Cleric testimony</title><p>Every original message must be recovered.</p>')
    image=io.BytesIO();Image.new('RGB',(8,8),'blue').save(image,format='GIF');(folder/'avatar.php?userid=1').write_bytes(image.getvalue())
    git(repo,'add','.');git(repo,'commit','-qm','Opaque dynamic routes')
    inv=job.TextInventory(repo,tmp_path/'state/x.sqlite');client=Client()
    try:
        p=job.index_websites(client,'eq',inv,embedder(),hosts=[HOST])
        assert p.created==2 and p.unsupported==1 and p.remaining_gaps==0
        assert str(opaque.relative_to(repo)) in client.docs
        assert 'original message must be recovered' in client.docs[str(opaque.relative_to(repo))]['text_full']
        assert inv.db.execute('SELECT reason FROM gaps').fetchone()[0]=='unsupported_media:image/gif'
    finally:inv.close()
