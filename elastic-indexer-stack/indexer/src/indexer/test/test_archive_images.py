from datetime import datetime,timedelta
import io
from pathlib import Path
import subprocess
from unittest.mock import MagicMock

from PIL import Image
import pytest

from indexer.archive_images import Inventory
from indexer import gemma
import index_images as job


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()


def repository(tmp_path,count=3):
    repo=tmp_path/'archive';repo.mkdir()
    git(repo,'init','-q');git(repo,'config','user.name','Fixture');git(repo,'config','user.email','fixture@example.org')
    stream=io.BytesIO();Image.new('RGB',(32,32),'blue').save(stream,format='PNG')
    for i in range(count):
        timestamp=(datetime(2000,1,1)+timedelta(seconds=i)).strftime('%Y%m%d%H%M%S')
        file=repo/'websites'/'guild.example'/timestamp/'screenshot.png'
        file.parent.mkdir(parents=True);file.write_bytes(stream.getvalue())
    other=repo/'websites'/'guild.example'/'20000101000000'/'page.html';other.write_text('Original HTML')
    git(repo,'add','.');git(repo,'commit','-qm','Known source snapshot')
    return repo


class Client:
    def __init__(self):self.docs={}
    def mget(self,*,index,ids,source):
        assert source is False
        return {'docs':[{'_id':key,'found':key in self.docs} for key in ids]}
    def create(self,*,index,id,document):
        self.docs[id]=document;return {'result':'created'}


def test_streamed_inventory_resumes_without_cloning_and_reuses_identical_images(tmp_path):
    repo=repository(tmp_path);state=tmp_path/'private/images.sqlite';client=Client();embedder=MagicMock()
    embedder.embed_image.return_value=[1.]+[0.]*767
    original=git(repo,'rev-parse','HEAD')
    inventory=Inventory(repo,state)
    try:
        progress=job.index_images(client,'eq',inventory,embedder,limit=1)
        assert progress.created==1
        assert not list(inventory.git('remote').splitlines())
    finally:inventory.close()
    inventory=Inventory(repo,state)
    try:
        progress=job.index_images(client,'eq',inventory,embedder)
        assert progress.created==2 and progress.cached==2
        assert not list(inventory.pending_hosts())
        assert embedder.embed_image.call_count==1
        assert len(client.docs)==3
        assert all(d['embedding_model']==gemma.MODEL and d['file_type']=='image' for d in client.docs.values())
    finally:inventory.close()
    assert git(repo,'rev-parse','HEAD')==original
    assert git(repo,'status','--porcelain')==''


def test_resume_limit_does_not_rewind_at_the_next_metadata_batch(tmp_path):
    repo=repository(tmp_path,count=55);inventory=Inventory(repo,tmp_path/'state/images.sqlite')
    client=Client();embedder=MagicMock();embedder.embed_image.return_value=[1.]+[0.]*767
    try:
        first=job.index_images(client,'eq',inventory,embedder,limit=50)
        second=job.index_images(client,'eq',inventory,embedder,limit=1)
        assert first.created==50 and second.created==1
        assert second.existing==0
        assert len(client.docs)==51
    finally:inventory.close()


def test_existing_ids_are_never_overwritten_or_reembedded(tmp_path):
    repo=repository(tmp_path,count=1);inventory=Inventory(repo,tmp_path/'state/images.sqlite')
    key='websites/guild.example/20000101000000/screenshot.png'
    client=Client();client.docs[key]={'original':'preserved'};embedder=MagicMock()
    try:
        progress=job.index_images(client,'eq',inventory,embedder)
        assert progress.existing==1 and progress.created==0
        assert client.docs[key]=={'original':'preserved'}
        embedder.embed_image.assert_not_called()
    finally:inventory.close()


def test_changed_source_revision_or_concurrent_writer_is_rejected(tmp_path):
    repo=repository(tmp_path);state=tmp_path/'state/images.sqlite';inventory=Inventory(repo,state)
    try:
        with pytest.raises(ValueError,match='already owns'):Inventory(repo,state)
    finally:inventory.close()
    (repo/'extra.txt').write_text('new revision');git(repo,'add','.');git(repo,'commit','-qm','Different source')
    with pytest.raises(ValueError,match='revision changed'):Inventory(repo,state)


def test_modified_worktree_cannot_be_published_under_the_committed_image_id(tmp_path):
    repo=repository(tmp_path,count=1);inventory=Inventory(repo,tmp_path/'state/images.sqlite')
    path=repo/'websites/guild.example/20000101000000/screenshot.png';path.write_bytes(b'changed')
    client=Client()
    try:
        with pytest.raises(ValueError,match='pinned archive'):job.index_images(client,'eq',inventory,MagicMock())
        assert client.docs=={}
        assert list(inventory.pending_hosts())[0][2]==0
    finally:inventory.close()


def test_missing_worktree_image_uses_only_the_existing_local_blob_cache(tmp_path):
    repo=repository(tmp_path,count=1);inventory=Inventory(repo,tmp_path/'state/images.sqlite')
    path=repo/'websites/guild.example/20000101000000/screenshot.png';path.unlink()
    client=Client();embedder=MagicMock();embedder.embed_image.return_value=[1.]+[0.]*767
    try:
        result=job.index_images(client,'eq',inventory,embedder)
        assert result.created==1 and result.missing==0
        assert not path.exists()
    finally:inventory.close()


def test_unsafe_symlink_and_incomplete_existing_lookup_do_not_advance_checkpoints(tmp_path):
    repo=repository(tmp_path,count=1);inventory=Inventory(repo,tmp_path/'state/images.sqlite')
    client=MagicMock();client.mget.return_value={'docs':[]}
    try:
        with pytest.raises(ValueError,match='Incomplete existing'):job.index_images(client,'eq',inventory,MagicMock())
        assert list(inventory.pending_hosts())[0][2]==0
        path=repo/'websites/guild.example/20000101000000/screenshot.png'
        copied=tmp_path/'copy.png';copied.write_bytes(path.read_bytes());path.unlink();path.symlink_to(copied)
        with pytest.raises(ValueError,match='symlink'):job.index_images(Client(),'eq',inventory,MagicMock())
    finally:inventory.close()


def test_missing_sources_stay_visible_on_restart_and_are_retried_before_new_work(tmp_path,monkeypatch):
    repo=repository(tmp_path,count=1);inventory=Inventory(repo,tmp_path/'state/images.sqlite')
    real=job.document
    monkeypatch.setattr(job,'document',MagicMock(side_effect=FileNotFoundError()))
    try:
        result=job.index_images(Client(),'eq',inventory,MagicMock())
        assert result.missing==1 and inventory.missing_sources()==1
        monkeypatch.setattr(job,'document',real)
        embedder=MagicMock();embedder.embed_image.return_value=[1.]+[0.]*767
        result=job.index_images(Client(),'eq',inventory,embedder)
        assert result.created==1 and result.missing==0
    finally:inventory.close()


def test_invalid_timestamp_is_a_per_file_diagnostic_and_other_images_continue(tmp_path):
    repo=repository(tmp_path,count=1)
    folder=repo/'websites/guild.example/20009999000000';folder.mkdir()
    (folder/'bad-date.png').write_bytes((repo/'websites/guild.example/20000101000000/screenshot.png').read_bytes())
    git(repo,'add','.');git(repo,'commit','-qm','Include malformed legacy timestamp')
    inventory=Inventory(repo,tmp_path/'state/images.sqlite')
    embedder=MagicMock();embedder.embed_image.return_value=[1.]+[0.]*767
    try:
        progress=job.index_images(Client(),'eq',inventory,embedder)
        assert progress.created==1 and progress.unsupported==1
        assert inventory.db.execute('SELECT reason FROM gaps').fetchone()[0]=='invalid_capture_identity'
    finally:inventory.close()
