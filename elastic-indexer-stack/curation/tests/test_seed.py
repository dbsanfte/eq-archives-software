import json
import os
from pathlib import Path

import pytest

from common import CrawlError
from seed import seed
from state import connect
from conftest import add_candidate


def test_seed_only_copies_manifest_files_and_preserves_source_and_review_on_restart(tmp_path):
    source=tmp_path/'pilot'
    row=add_candidate(source)
    (source/'orphan.html').write_text('not listed')
    destination=tmp_path/'production'
    seed(source,destination)
    assert (destination/'indexing-state').stat().st_mode&0o7777==0o2770
    assert (destination/row['captures'][0]['path']).read_bytes() == (source/row['captures'][0]['path']).read_bytes()
    assert not (destination/'orphan.html').exists()
    assert not (destination/'git-reader').exists()
    with connect(destination) as store:
        store.db.execute("UPDATE candidates SET state='deferred'")
        store.db.commit()
    seed(source,destination)
    with connect(destination) as store:
        assert store.db.execute('SELECT state FROM candidates').fetchone()[0] == 'deferred'
    with connect(source) as store:
        assert store.db.execute('SELECT state FROM candidates').fetchone()[0] == 'approval_pending'


def test_existing_seed_marker_still_prepares_private_shared_indexing_state_without_reseeding(tmp_path):
    source=tmp_path/'absent-pilot';root=tmp_path/'production';root.mkdir();(root/'seed-complete').touch()
    seed(source,root)
    assert (root/'indexing-state').is_dir() and (root/'indexing-state').stat().st_mode&0o7777==0o2770
    assert not (root/'crawl.sqlite3').exists()


@pytest.mark.parametrize('existing_mode',[None,0o700,0o2770])
def test_shared_directory_permissions_work_without_fowner_on_first_start_and_restart(tmp_path,monkeypatch,existing_mode):
    root=tmp_path/'production';root.mkdir();(root/'seed-complete').touch()
    indexing=root/'indexing-state';owners={}
    if existing_mode is not None:
        indexing.mkdir();indexing.chmod(existing_mode);owners[indexing]=10001
    original=Path.chmod
    def chmod(path,mode,*args,**kwargs):
        if path==indexing and owners.get(path,os.geteuid())!=os.geteuid():
            raise PermissionError('CHOWN does not grant FOWNER')
        return original(path,mode,*args,**kwargs)
    def chown(path,uid,gid):
        assert path==indexing and gid==10001
        owners[path]=uid
    monkeypatch.setattr(Path,'chmod',chmod);monkeypatch.setattr(os,'chown',chown)
    seed(tmp_path/'absent-pilot',root,10001)
    seed(tmp_path/'absent-pilot',root,10001)
    assert owners[indexing]==10001 and indexing.stat().st_mode&0o7777==0o2770
    assert not (root/'crawl.sqlite3').exists()


def test_interrupted_seed_rechecks_artifacts_before_marking_complete(tmp_path):
    source=tmp_path/'pilot'
    row=add_candidate(source)
    path=source/row['captures'][0]['path']
    original=path.read_bytes()
    path.write_bytes(b'changed')
    destination=tmp_path/'production'
    with pytest.raises(CrawlError): seed(source,destination)
    assert not (destination/'seed-complete').exists()
    path.write_bytes(original)
    seed(source,destination)
    assert (destination/'seed-complete').exists()
    assert (destination/row['captures'][0]['path']).read_bytes() == original
