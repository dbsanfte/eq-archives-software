"""Discovery must retain its own evidence without cloning the archive graph."""
import json

import pytest

from common import CrawlError
from campaign_storage import initialize
from discovery_cache import SHARED_GRAPH_VERSION, link_rows, shared_graph_root
from graph import staged_links
from state import connect
from worker import campaign


def test_new_campaign_does_not_copy_shared_graph_or_other_operations(candidate, monkeypatch):
    root, known = candidate
    with connect(root) as main:
        main.db.executemany('INSERT INTO links VALUES (?,?,?)', [
            (f'http://guild{n}.example/', 'archived-source', json.dumps({'context': 'x' * 1024 * 1024}))
            for n in range(12)])
        main.db.execute("INSERT INTO files VALUES ('tree','page','blob')")
        main.db.execute("INSERT INTO scans VALUES ('blob','source','{}')")
        main.db.execute("INSERT INTO tree_state VALUES ('tree',1,1)")
        main.db.execute("INSERT INTO hosts VALUES ('seed.example','tree')")
        main.db.execute("INSERT INTO ezboard_aliases VALUES ('forum','board','http://pub1.ezboard.com/bboard','{}')")
        main.db.execute("INSERT INTO attempts VALUES (1,'previous','signature',2,NULL,'uncertain','now')")
        main.set('fill_checkpoint', {'deadline': 1, 'accepted': ['previous']})
        main.set('automatic_settings', {'enabled': True, 'revision': 12})
        main.set('archive_sha', 'a' * 40)
        main.set('ezboard_archive_inventory:v1:' + 'a' * 40, {'checked': 5})

    def inspect(_root, _operation, run):
        for table in ('links', 'scans', 'files', 'tree_state', 'candidates', 'attempts', 'events'):
            assert run.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert run.db.execute('SELECT host FROM hosts').fetchone()[0] == 'seed.example'
        assert run.db.execute('SELECT board FROM ezboard_aliases').fetchone()[0] == 'board'
        assert 'http://guild.example/' in run.get('excluded_scopes')
        assert run.get('fill_checkpoint') is None
        assert run.get('automatic_settings') is None
        assert run.get('archive_sha') == 'a' * 40
        assert run.get('ezboard_archive_inventory:v1:' + 'a' * 40) == {'checked': 5}
        assert (run.root / 'crawl.sqlite3').stat().st_size < 1024 * 1024
        return {'checked': True}

    monkeypatch.setattr('worker.fill', inspect)
    assert campaign(root, {'id': 'a' * 32, 'payload': {'fill_queue': True}}) == {'checked': True}
    with connect(root) as main:
        assert main.db.execute('SELECT COUNT(*) FROM links').fetchone()[0] == 12
        assert main.db.execute('SELECT reserved,actual FROM attempts').fetchone()[:] == (2, None)


def test_initialized_campaign_retains_budget_deadline_and_evidence(candidate, monkeypatch):
    root, _ = candidate
    directory = root / 'runs' / ('b' * 32)
    with connect(directory) as run:
        run.set('fill_checkpoint', {'deadline': 123, 'accepted': ['saved']})
        run.db.execute("INSERT INTO attempts VALUES (1,'saved','paid-signature',2,1,'received','now')")
        run.db.execute("INSERT INTO links VALUES ('http://old.example/','exact-source','{}')")
        run.db.commit()
        (directory / 'initialized').touch()
        (directory / 'evidence.html').write_bytes(b'original evidence')

    def resume(_root, _operation, run):
        assert run.get('fill_checkpoint') == {'deadline': 123, 'accepted': ['saved']}
        assert run.db.execute('SELECT reserved,actual FROM attempts').fetchone()[:] == (2, 1)
        assert run.db.execute('SELECT source FROM links').fetchone()[0] == 'exact-source'
        assert (run.root / 'evidence.html').read_bytes() == b'original evidence'
        return {'resumed': True}

    monkeypatch.setattr('worker.fill', resume)
    assert campaign(root, {'id': 'b' * 32, 'payload': {'fill_queue': True}}) == {'resumed': True}


def test_interrupted_initialization_is_replaced_without_copying_parent_reservations(candidate, monkeypatch):
    root, _ = candidate
    directory = root / 'runs' / ('c' * 32)
    # An old release could stop after backing up the parent, before its marker.
    with connect(directory) as old:
        old.db.execute("INSERT INTO attempts VALUES (1,'old','copied',2,NULL,'uncertain','now')")
        old.db.commit()
    import campaign_storage
    replace = campaign_storage.os.replace
    monkeypatch.setattr(campaign_storage.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        initialize(root, directory)
    assert not (directory / 'initialized').exists()
    monkeypatch.setattr(campaign_storage.os, 'replace', replace)
    initialize(root, directory)
    with connect(directory) as run:
        assert run.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
        assert run.get('campaign_storage') == SHARED_GRAPH_VERSION
        assert run.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not list(directory.glob('.initialize-*'))


def test_shared_graph_reads_new_links_without_copying_and_preserves_local_evidence(candidate):
    root, _ = candidate
    directory = root / 'runs' / ('d' * 32)
    initialize(root, directory)
    with connect(directory) as run, connect(root) as main:
        main.db.executemany('INSERT INTO links VALUES (?,?,?)', [
            ('http://guild.example/', 'same-source', '{"version":"shared"}'),
            ('http://guild.example/', 'other-source', '{"version":"independent"}'),
            ('http://new.example/', 'later-source', '{"version":"later"}')])
        main.db.commit()
        run.db.execute("INSERT INTO links VALUES ('http://guild.example/','same-source','{\"version\":\"local\"}')")
        run.db.commit()
        rows = list(link_rows(run))
        assert {(r['url'], r['source']) for r in rows} == {
            ('http://guild.example/', 'same-source'), ('http://guild.example/', 'other-source'),
            ('http://new.example/', 'later-source')}
        assert next(json.loads(r['evidence']) for r in rows if r['source'] == 'same-source') == {'version': 'local'}
        assert run.db.execute('SELECT COUNT(*) FROM links').fetchone()[0] == 1
        assert main.db.execute('SELECT COUNT(*) FROM links').fetchone()[0] == 3


def test_missing_shared_graph_fails_visibly_without_creating_an_empty_database(tmp_path):
    root = tmp_path / 'missing'
    with connect(root / 'runs' / ('e' * 32)) as run:
        run.set('campaign_storage', SHARED_GRAPH_VERSION)
        with pytest.raises(CrawlError, match='Shared discovery graph is unavailable'):
            list(link_rows(run))
        assert not (root / 'crawl.sqlite3').exists()
    with connect(tmp_path / 'wrong-location') as run:
        run.set('campaign_storage', SHARED_GRAPH_VERSION)
        with pytest.raises(CrawlError, match='Invalid shared discovery campaign location'):
            shared_graph_root(run)


@pytest.mark.parametrize('empty', [False, True])
def test_missing_initialized_ledger_cannot_reset_paid_budget(candidate, empty):
    root, _ = candidate
    directory = root / 'runs' / ('2' * 32)
    directory.mkdir(parents=True)
    (directory / 'initialized').touch()
    if empty:
        (directory / 'crawl.sqlite3').touch()
    with pytest.raises(CrawlError, match='Initialized campaign ledger is missing'):
        campaign(root, {'id': '2' * 32, 'payload': {'fill_queue': True}})


def test_staged_graph_is_shared_across_new_runs_without_rereading_sources(candidate):
    root, _ = candidate
    from conftest import add_candidate
    add_candidate(root, url='http://links.example/', body=b'<p>EverQuest guild history.</p><a href="http://new.example/">Guild</a>')
    first = root / 'runs' / ('f' * 32)
    second = root / 'runs' / ('1' * 32)
    initialize(root, first)
    with connect(first) as run:
        assert staged_links(root, run)['staged_source_reads'] == 2
        assert run.db.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 0
        assert any(row['url'] == 'http://new.example/' for row in link_rows(run))
    initialize(root, second)
    with connect(second) as run:
        assert staged_links(root, run)['staged_source_reads'] == 0
        assert run.db.execute('SELECT COUNT(*) FROM links').fetchone()[0] == 0
        assert any(row['url'] == 'http://new.example/' for row in link_rows(run))
        with pytest.raises(CrawlError, match='does not match staged source storage'):
            staged_links(root / 'other', run)
