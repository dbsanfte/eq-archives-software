import json
from unittest.mock import Mock

import pytest

from common import CrawlError, Store
from conftest import add_candidate
from discovery_run import fill
from server import create_app
from state import connect, enqueue, unpack
from test_server import call
from worker import campaign


@pytest.fixture
def run_fixture(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    clock = [10000.0]
    monkeypatch.delenv('ARCHIVE_REPO', raising=False)
    monkeypatch.setattr('discovery_run.time.time', lambda: clock[0])
    client = Mock()
    downloader = Mock()
    monkeypatch.setattr('discovery_run.Luna', Mock(return_value=client))
    monkeypatch.setattr('discovery_run.Downloader', Mock(return_value=downloader))
    staged = Mock(return_value={'staged_source_reads': 0, 'staged_source_bytes': 0})
    monkeypatch.setattr('discovery_run.staged_links', staged)
    saved = {}
    discovered = []
    configuration = {'available': 150, 'bad_grades': 0, 'unavailable': 0, 'seconds': 1}
    def discover(args, store, **kwargs):
        discovered.append((args.max_candidates, kwargs))
        for index in range(len(store.candidates()), min(configuration['available'], args.max_candidates)):
            row = add_candidate(store.root, url=f'http://guild-{index}.example/', grade=None)
            saved[row['id']] = row['captures']
            store.db.execute("UPDATE candidates SET captures='[]',state='discovered' WHERE id=?", (row['id'],))
            store.db.commit()
    monkeypatch.setattr('discovery_run.discover', discover)
    observed = []
    def sample(args, store, *, candidates, downloader):
        with connect(root) as main:
            observed.append(len(main.candidates()))
        clock[0] += configuration['seconds']
        row = candidates[0]
        index = int(row['url'].split('guild-')[1].split('.')[0])
        captures = [] if index < configuration['unavailable'] else saved[row['id']]
        store.db.execute("UPDATE candidates SET captures=?,state=? WHERE id=?", (json.dumps(captures), 'sampled' if captures else 'unavailable', row['id']))
        store.db.commit()
    monkeypatch.setattr('discovery_run.sample', sample)
    def response(payload):
        index = int(json.loads(payload['input'])['captures'][0]['url'].split('guild-')[1].split('.')[0])
        rating = {'grade': 0 if index < configuration['bad_grades'] else 3, 'category': 'guild', 'confidence': 'high',
                  'reason': 'EQ guild.', 'evidence': [{'slot': 0, 'excerpt': 'EverQuest guild history.'}]}
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(rating)}]}],
                'usage': {'input_tokens': 100, 'output_tokens': 100}}
    client.request.side_effect = response
    def operation(target=50, budget=2, minimum=2):
        with connect(root) as main:
            identifier = enqueue(main, 'discover', {'max_candidates': target, 'max_usd': budget, 'min_grade': minimum, 'fill_queue': True})
            return unpack(main.db.execute('SELECT * FROM operations WHERE id=?', (identifier,)).fetchone())
    return root, clock, client, downloader, configuration, operation, observed, discovered


def test_fill_continues_past_low_grades_and_publishes_each_result_while_running(run_fixture):
    root, clock, client, downloader, settings, operation, observed, discovered = run_fixture
    settings.update(bad_grades=10, unavailable=2)
    op = operation()
    result = campaign(root, op)['progress']
    assert result['accepted'] == 50 and result['checked'] == 60 and result['stop_reason'] == 'target_reached'
    assert client.request.call_count == 58
    assert observed[:3] == [0, 1, 2] and observed[-1] == 59  # Live before completion.
    assert discovered[0][1]['cached_only'] is False and discovered[1][1]['cached_only'] is True
    assert downloader.close.call_count == client.close.call_count == 1
    with connect(root) as main:
        assert len(main.candidates()) == 60
        assert all(row['decision'] is None for row in main.candidates())
        assert main.db.execute('SELECT COUNT(*) FROM events WHERE action=\'discovery_result\'').fetchone()[0] == 60
    app = create_app(root, start_worker=False)
    listing = call(app, 'GET', '/api/queue?filter=candidates&min_grade=2').json()
    assert listing['total'] == 50 and listing['operations'][0]['result']['progress']['accepted'] == 50
    # A replay of the finished run does not spend, rescan, or alter human state.
    with connect(root) as main:
        first = main.candidates()[0]
        main.db.execute("UPDATE candidates SET state='rejected' WHERE id=?", (first['id'],));main.db.commit()
    assert campaign(root, op)['progress']['accepted'] == 50
    assert client.request.call_count == 58 and len(discovered) == 2
    with connect(root) as main:
        assert main.db.execute('SELECT state FROM candidates WHERE id=?', (first['id'],)).fetchone()[0] == 'rejected'


def test_discovery_criteria_are_saved_for_every_grade_and_locked_on_resume(run_fixture):
    root, clock, client, downloader, settings, operation, observed, discovered = run_fixture
    app = create_app(root, start_worker=False)
    reply = call(app, 'POST', '/api/discover', {'max_candidates': 2, 'max_usd': .25, 'min_grade': 2,
                                              'grading_criteria': '  Cleric class sites  '})
    assert reply.status_code == 202
    with connect(root) as main:
        op = unpack(main.db.execute('SELECT * FROM operations').fetchone())
        assert op['payload']['grading_criteria'] == 'Cleric class sites'
        main.db.execute("UPDATE operations SET state='interrupted'");main.db.commit()
    assert call(app, 'POST', '/api/resume', {'id': op['id'], 'grading_criteria': 'Guild sites'}).status_code == 409
    assert call(app, 'POST', '/api/resume', {'id': op['id']}).status_code == 202
    result = campaign(root, op)['progress']
    assert result['accepted'] == 2
    for requested in client.request.call_args_list:
        assert json.loads(requested.args[0]['input'])['grading_criteria'] == 'Cleric class sites'
    with connect(root) as main:
        for row in main.candidates():
            assert json.loads(row['rating'])['grading_criteria'] == 'Cleric class sites'
        assert unpack(main.db.execute('SELECT * FROM operations').fetchone())['payload'] == op['payload']


def test_fill_stops_at_original_hour_deadline_and_resume_never_resets_it(run_fixture):
    root, clock, client, downloader, settings, operation, observed, discovered = run_fixture
    settings['seconds'] = 3601
    op = operation()
    result = campaign(root, op)['progress']
    assert result['stop_reason'] == 'time_limit' and result['accepted'] == 0 and result['checked'] == 1
    client.request.assert_not_called()
    clock[0] += 100
    resumed = campaign(root, op)['progress']
    assert resumed['deadline'] == 13600 and resumed['stop_reason'] == 'time_limit'
    assert len(discovered) == 1 and len(observed) == 1


def test_fill_stops_when_next_request_would_exceed_total_dollar_cap(run_fixture):
    root, clock, client, downloader, settings, operation, observed, discovered = run_fixture
    op = operation(budget=.004)
    result = campaign(root, op)['progress']
    assert result['stop_reason'] == 'spend_limit' and result['accepted'] == 1
    assert result['reserved_usd'] <= .004 and client.request.call_count == 1
    campaign(root, op)
    assert client.request.call_count == 1


def test_fill_reports_exhausted_links_and_respects_minimum_zero(run_fixture):
    root, clock, client, downloader, settings, operation, observed, discovered = run_fixture
    settings.update(available=2, bad_grades=2)
    result = campaign(root, operation(minimum=0))['progress']
    assert result['accepted'] == 2 and result['stop_reason'] == 'links_exhausted'


def test_fill_excludes_concurrent_duplicates_before_spending(run_fixture):
    root, clock, client, downloader, settings, operation, observed, discovered = run_fixture
    settings['available'] = 3
    add_candidate(root, url='https://www.guild-0.example/')
    result = campaign(root, operation(target=2))['progress']
    assert result['accepted'] == 2 and result['skipped'] == 1
    assert client.request.call_count == 2
    with connect(root) as main:
        assert len(main.candidates()) == 3


def test_interruption_after_merge_reuses_receipt_without_overwriting_or_spending(run_fixture, monkeypatch):
    import discovery_run
    root, clock, client, downloader, settings, operation, observed, discovered = run_fixture
    original = discovery_run.merge_site
    fail = [True]
    def interrupted(*args):
        result = original(*args)
        if fail[0]:
            fail[0] = False
            raise CrawlError('Worker interrupted after merge')
        return result
    monkeypatch.setattr('discovery_run.merge_site', interrupted)
    op = operation(target=2)
    with pytest.raises(CrawlError, match='interrupted'):
        campaign(root, op)
    with connect(root) as main:
        first = main.candidates()[0]
        main.db.execute("UPDATE candidates SET state='rejected' WHERE id=?", (first['id'],));main.db.commit()
        progress = unpack(main.db.execute('SELECT * FROM operations').fetchone())['result']['progress']
        assert progress['phase'] == 'paused'
    result = campaign(root, op)['progress']
    assert result['accepted'] == 2 and client.request.call_count == 2
    with connect(root) as main:
        assert main.db.execute('SELECT state FROM candidates WHERE id=?', (first['id'],)).fetchone()[0] == 'rejected'


def test_lost_paid_response_is_not_automatically_charged_again_on_resume(run_fixture, monkeypatch):
    root, clock, client, downloader, settings, operation, observed, discovered = run_fixture
    op = operation(target=1)
    response = client.request.side_effect
    client.request.side_effect = RuntimeError('process stopped after reservation')
    with pytest.raises(RuntimeError):
        campaign(root, op)
    client.request.side_effect = response
    result = campaign(root, op)['progress']
    assert result['accepted'] == 1 and result['checked'] == 2 and client.request.call_count == 2
    with connect(root) as main:
        assert sum(row['state'] == 'grade_error' for row in main.candidates()) == 1


@pytest.mark.parametrize('minimum', [-1, 4, '2', True, None])
def test_discovery_minimum_grade_is_validated(tmp_path, minimum):
    app = create_app(tmp_path / 'state', start_worker=False)
    assert call(app, 'POST', '/api/discover', {'max_candidates': 50, 'max_usd': 2, 'min_grade': minimum}).status_code == 409


def test_source_change_after_grading_is_not_counted_or_auto_regraded(run_fixture, monkeypatch):
    import discovery_run
    root, clock, client, downloader, settings, operation, observed, discovered = run_fixture
    settings['available'] = 2
    original = discovery_run.grade
    def tamper(args, store, **kwargs):
        original(args, store, **kwargs)
        row = kwargs['candidates'][0]
        if 'guild-0.' in row['url']:
            path = json.loads(row['captures'])[0]['path']
            (store.root / path).write_bytes(b'changed')
    monkeypatch.setattr('discovery_run.grade', tamper)
    result = campaign(root, operation(target=2))['progress']
    assert result['accepted'] == 1 and result['stop_reason'] == 'links_exhausted'
    assert client.request.call_count == 2
    with connect(root) as main:
        failed = next(row for row in main.candidates() if 'guild-0.' in row['url'])
        assert failed['state'] == 'grade_error' and not failed['rating']


def test_metadata_cache_merge_preserves_newer_archive_snapshot(tmp_path):
    from discovery_run import retain_graph
    root = tmp_path / 'state'
    with connect(root) as main:
        main.set('archive_sha', 'new')
    run = Store(tmp_path / 'run')
    try:
        run.set('archive_sha', 'old')
        run.db.execute("INSERT INTO hosts VALUES ('new.example','tree')");run.db.commit()
        retain_graph(root, run)
        with connect(root) as main:
            assert not main.db.execute('SELECT 1 FROM hosts').fetchone()
        run.set('archive_sha', 'new')
        retain_graph(root, run)
        retain_graph(root, run)
        with connect(root) as main:
            assert main.db.execute('SELECT COUNT(*) FROM hosts').fetchone()[0] == 1
    finally:
        run.close()


def test_fill_resolves_ezboard_threads_before_creating_one_board_candidate(run_fixture,monkeypatch):
    root,clock,client,downloader,settings,operation,observed,discovered=run_fixture
    board='http://pub4.ezboard.com/beqasylum'
    message='http://pub110.ezboard.com/feqasylumgeneral.showMessage?topicID=2.topic'
    calls=[]
    def discover_board(args,store,**kwargs):
        calls.append(kwargs)
        if not store.get('resolved_board'):
            store.set('ezboard_pending_links',[message]);return
        row=add_candidate(store.root,url=board,grade=None)
        store.db.execute('UPDATE candidates SET coverage=? WHERE id=?',
                         (json.dumps({'scope_mode':'ezboard','site_check':{'status':'new_site','complete':True}}),row['id']))
        store.db.commit();store.set('ezboard_pending_links',[])
    def resolve_board(store,url,transport):
        assert url==message and transport is downloader
        client.request.assert_not_called()
        assert not store.candidates()
        store.set('resolved_board',board)
        return board
    monkeypatch.setattr('discovery_run.discover',discover_board)
    resolver=Mock(side_effect=resolve_board)
    monkeypatch.setattr('ezboard_discovery.resolve',resolver)
    client.request.side_effect=None
    client.request.return_value={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({
        'grade':3,'category':'forum','confidence':'high','reason':'EverQuest forum evidence.',
        'evidence':[{'slot':0,'excerpt':'EverQuest guild history.'}]})}]}],'usage':{'input_tokens':100,'output_tokens':100}}
    result=campaign(root,operation(target=1))
    assert result['progress']['accepted']==1 and result['progress']['stop_reason']=='target_reached'
    assert resolver.call_count==1 and client.request.call_count==1 and downloader.close.call_count==1
    assert len(calls)==2
    with connect(root) as main:
        rows=main.candidates()
        assert len(rows)==1 and rows[0]['url']==board and rows[0]['scope']==board
