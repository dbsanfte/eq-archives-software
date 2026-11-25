import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from capture_rank import estimate_query, measure_next
from conftest import add_candidate
from state import connect
from test_downloader import FixtureServer
from wayback_transport import SharedWayback


def test_wayback_size_uses_scoped_distinct_pages_and_resumes_in_next_turn(tmp_path):
    root = tmp_path / 'state'
    site = add_candidate(root, 'http://geocities.com/Account/guide.html')
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='approved_waiting_batch' WHERE id=?", (site['id'],))
        store.db.commit()
    with FixtureServer() as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        transport = SharedWayback(threading.Event(), f'http://127.0.0.1:{server.server_port}')
        header = server.catalog[0]
        def record(url, timestamp='20000101000000', mime='text/html'):
            return [timestamp, url, mime, '200', 'D', '80']
        try:
            url = 'http://geocities.com/Account/guide.html'
            server.catalog = [header, record(url), record(url,'20050101000000'),
                              record('http://geocities.com/AccountOther/raid.html'), [], ['cursor%21']]
            measure_next(root, transport)
            with connect(root) as store:
                estimate = store.db.execute('SELECT pages,resume_key FROM capture_size_estimates').fetchone()
                assert tuple(estimate) == (1,'cursor%21')
            server.catalog = [header, record(url), record('http://geocities.com/Account/log.txt',mime='text/plain')]
            measure_next(root, transport)
            measure_next(root, transport)
            with connect(root) as store:
                assert tuple(store.db.execute('SELECT pages,resume_key,retry_at FROM capture_size_estimates').fetchone()) == (2,None,None)
            assert len(server.requests) == 2 and server.connections == 1
            queries = [parse_qs(urlsplit(request).query) for request,_ in server.requests]
            assert all(query['collapse'] == ['urlkey'] and query['limit'] == ['1000'] and
                       query['matchType'] == ['prefix'] and query['url'] == ['http://geocities.com/Account/'] and
                       query['from'] == ['19990101000000'] and query['to'] == ['20061231235959'] for query in queries)
            assert queries[1]['resumeKey'] == ['cursor!']
        finally:
            transport.close()
            server.shutdown()
            thread.join(timeout=2)


def test_failed_size_probe_backs_off_and_leaves_the_approval_eligible(tmp_path):
    from capture_queue import next_entry
    from common import now

    root = tmp_path / 'state'
    site = add_candidate(root)
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='approved_waiting_batch',decision=? WHERE id=?",
                         (json.dumps({'capture_after':'2020-01-01'}),site['id']))
        store.db.commit()
    with FixtureServer() as server:
        server.statuses = [403]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        transport = SharedWayback(threading.Event(), f'http://127.0.0.1:{server.server_port}')
        try:
            measure_next(root, transport)
            measure_next(root, transport)
            assert len(server.requests) == 1
            with connect(root) as store:
                result = store.db.execute('SELECT pages,retry_at FROM capture_size_estimates').fetchone()
                assert result['pages'] is None and result['retry_at'] > now()
                assert next_entry(store,now())['id'] == site['id']
        finally:
            transport.close()
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize('mode,scope,expected,pages', [
    ('ezboard','http://pub3.ezboard.com/beq','http://pub3.ezboard.com/feq',[
        ('http://pub3.ezboard.com/feqmain.showMessage?topicID=2.topic',True),
        ('http://pub3.ezboard.com/feqmainother.showMessage?topicID=2.topic',False),
        ('http://pub4.ezboard.com/feqmain.showMessage?topicID=2.topic',False)]),
    ('sitepowerup','http://www.sitepowerup.com/mb/view.asp?BoardID=102010',
     'http://www.sitepowerup.com/mb/view.asp?BoardID=102010',[
        ('http://www.sitepowerup.com/mb/view.asp?Action=Reply&BoardID=102010&Reply=12155',True),
        ('http://www.sitepowerup.com/mb/view.asp?Action=Reply&BoardID=1020100&Reply=12155',False)])])
def test_board_estimate_only_counts_urls_within_the_board(tmp_path,mode,scope,expected,pages):
    with connect(tmp_path / 'state') as store:
        store.db.execute('INSERT INTO ezboard_aliases VALUES (?,?,?,?)',('feqmain','eq',scope,'{}'))
        query, member = estimate_query(store, {'mode':mode,'scope':scope,'captures':'[]'})
    assert query['url'] == expected
    assert [member(url) for url,_ in pages] == [allowed for _,allowed in pages]
