import json

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
