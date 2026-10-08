import json
import pytest
from pathlib import Path
from unittest.mock import patch

from common import CrawlError, digest
from conftest import add_candidate
from review import checked_sources
from server import create_app
from state import connect, unpack
from test_coverage_check import archive
from test_server import call
from worker import Worker


@pytest.mark.parametrize('focus', [None, [], {}, True, 12, 'a' * 1001, 'Cleric\x00sites', 'Guild\x7fsites'])
def test_invalid_criteria_cannot_start_paid_work(tmp_path, monkeypatch, focus):
    root, app, row, payload = setup(tmp_path, monkeypatch)
    for path, data in [('/api/check-candidate', payload),
                       ('/api/discover', {'max_candidates': 50, 'max_usd': 2}),
                       ('/api/submit-site', {'url': 'http://new.example/', 'max_usd': 2})]:
        reply = call(app, 'POST', path, {**data, 'grading_criteria': focus})
        assert reply.status_code == 409 and 'criteria' in reply.json()['error']
    with connect(root) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 0


def test_regrading_uses_original_budget_frozen_criteria_and_source_bound_caches(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch)
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client, patch('candidate_checks.sample') as download:
            client.return_value.request.return_value = response()
            operation = call(app, 'POST', '/api/check-candidate', payload).json()['operation']
            worker.operation()
            current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
            original_hash = current['manifest_sha256']
            regrade = {**payload, 'manifest_sha256': original_hash, 'max_usd': 2,
                       'grading_criteria': '  Guild sites\r\nwith raiding stories  '}
            result = call(app, 'POST', '/api/check-candidate', regrade)
            assert result.status_code == 202
            assert result.json()['operation'] == operation and result.json()['max_usd'] == .25
            changing = call(app, 'POST', '/api/check-candidate', {**regrade, 'grading_criteria': 'Cleric sites'})
            assert changing.status_code == 409 and 'already running' in changing.json()['error']
            # A concurrent approval must not use the superseded grade while a check runs.
            approval = call(app, 'POST', '/api/decisions', [{'id': row['id'], 'manifest_sha256': original_hash, 'decision': 'approve'}])
            assert approval.status_code == 409 and 'grading check' in approval.json()['error']
            assert call(app, 'POST', '/api/check-candidate', {**regrade, 'max_usd': 1}).json()['operation'] == operation
            worker.operation()
            assert client.return_value.request.call_count == 2
            assert json.loads(client.return_value.request.call_args.args[0]['input'])['grading_criteria'] == 'Guild sites\nwith raiding stories'
            current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
            assert current['rating']['grading_criteria'] == current['candidate_check']['grading_criteria'] == 'Guild sites\nwith raiding stories'
            assert current['manifest_sha256'] != original_hash and current['candidate_check']['max_usd'] == .25
            # Returning to an already paid assessment reuses it without another charge.
            result = call(app, 'POST', '/api/check-candidate', {**regrade, 'manifest_sha256': current['manifest_sha256'], 'grading_criteria': ''})
            assert result.json()['operation'] == operation
            worker.operation()
            assert client.return_value.request.call_count == 2
            current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
            assert current['rating']['grading_criteria'] == '' and current['manifest_sha256'] == original_hash
            download.assert_not_called()
            with connect(root / 'runs' / operation) as store:
                assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 2
    finally:
        worker.lease.close()


def test_received_invalid_grade_is_cached_and_retry_keeps_criteria(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch)
    operation = call(app, 'POST', '/api/check-candidate', {**payload, 'grading_criteria': 'Cleric sites'}).json()['operation']
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client:
            client.return_value.request.return_value = {'status': 'incomplete', 'usage': {'input_tokens': 40, 'output_tokens': 40}}
            worker.operation()
            current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
            assert current['state'] == 'grade_error'
            assert call(app, 'POST', '/api/check-candidate', {**payload, 'manifest_sha256': current['manifest_sha256']}).json()['operation'] == operation
            worker.operation()
            assert client.return_value.request.call_count == 1
            assert json.loads(client.return_value.request.call_args.args[0]['input'])['grading_criteria'] == 'Cleric sites'
        with connect(root / 'runs' / operation) as store:
            assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1
            assert store.db.execute('SELECT actual FROM attempts').fetchone()[0] > 0
    finally:
        worker.lease.close()


def test_legacy_paid_grade_can_be_restored_after_custom_regrade(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch, grade=3)
    # A pre-existing grade was paid in a different discovery ledger.
    current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client:
            client.return_value.request.return_value = response()
            assert call(app, 'POST', '/api/check-candidate', {**payload, 'manifest_sha256': current['manifest_sha256'], 'grading_criteria': 'Guild sites'}).status_code == 202
            worker.operation()
            current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
            assert call(app, 'POST', '/api/check-candidate', {**payload, 'manifest_sha256': current['manifest_sha256'], 'grading_criteria': ''}).status_code == 202
            worker.operation()
            assert client.return_value.request.call_count == 1
            current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
            assert not current['rating'].get('grading_criteria') and current['rating']['grade'] == 3
    finally:
        worker.lease.close()


def response():
    return {'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 100},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                'grade': 3, 'category': 'guild', 'confidence': 'high', 'reason': 'An EQ guild.',
                'evidence': [{'slot': 0, 'excerpt': 'EverQuest guild history.'}]})}]}]}


def setup(tmp_path, monkeypatch, samples=True, grade=None):
    root = tmp_path / 'state'
    row = add_candidate(root, grade=grade)
    if not samples:
        with connect(root) as store:
            store.db.execute("UPDATE candidates SET captures='[]',state='unavailable',error='No exact HTML captures found'")
            store.db.commit()
    repo = archive(tmp_path, ['known.example/20000101000000/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO', str(repo))
    app = create_app(root, start_worker=False)
    row = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
    return root, app, row, {'id': row['id'], 'manifest_sha256': row['manifest_sha256'], 'max_usd': .25}


def test_saved_samples_are_graded_without_redownload_and_budget_is_deduplicated(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch)
    result = call(app, 'POST', '/api/check-candidate', payload)
    assert result.status_code == 202
    operation = result.json()['operation']
    again = call(app, 'POST', '/api/check-candidate', {**payload, 'max_usd': 2})
    assert again.json()['operation'] == operation and again.json()['max_usd'] == .25
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client, patch('candidate_checks.sample') as download:
            client.return_value.request.return_value = response()
            worker.operation()
            download.assert_not_called()
            assert client.return_value.request.call_count == 1
        current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
        assert current['state'] == 'approval_pending' and current['rating']['grade'] == 3
        assert current['captures'] == row['captures'] and current['scope'] == row['scope']
        assert current['candidate_check']['state'] == 'completed'
        with connect(root) as store:
            assert checked_sources(store, current)[0]['complete_extracted_text'].endswith('Guide')
        source = root / row['captures'][0]['path']
        assert source.stat().st_ino == (root / 'runs' / operation / 'reused/0.html').stat().st_ino
        assert call(app, 'POST', '/api/check-candidate', {**payload, 'manifest_sha256': current['manifest_sha256']}).status_code == 409
    finally:
        worker.lease.close()


def test_sampling_failure_is_visible_and_retry_preserves_operation_and_transport_budget(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch, samples=False)
    operation = call(app, 'POST', '/api/check-candidate', payload).json()['operation']
    transport = []
    class Downloader:
        def __init__(self, store, args):
            assert args.max_candidates == 1 and args.delay == 3 and args.bytes_per_second == 131072
            transport.append(store.get('wayback_transport', {}))
            store.set('wayback_transport', {'requests': len(transport)})
        def call(self, request):
            if request['op'] == 'list':
                return {'captures': [] if len(transport) == 1 else [{'url': request['url'], 'timestamp': '20000101000000', 'digest': 'cdx', 'length': 100}],
                        'listing_limited': False, 'available_rows': 1, 'identity_variants': []}
            source = b'<p>EverQuest guild history.</p>'
            Path(request['destination']).write_bytes(source)
            return {'url': request['url'], 'timestamp': request['timestamp'], 'sha256': digest(source), 'bytes': len(source)}
        def close(self): pass
    monkeypatch.setattr('acquisition.Downloader', Downloader)
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client:
            client.return_value.request.return_value = response()
            worker.operation()
            client.assert_not_called()
            current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
            assert current['state'] == 'unavailable' and current['candidate_check']['state'] == 'interrupted'
            assert 'No exact HTML' in current['candidate_check']['error']
            retry = call(app, 'POST', '/api/check-candidate', {**payload, 'manifest_sha256': current['manifest_sha256'], 'max_usd': 2})
            assert retry.json()['operation'] == operation and retry.json()['max_usd'] == .25
            worker.operation()
            assert client.return_value.request.call_count == 1
        current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
        assert current['rating']['grade'] == 3 and current['state'] == 'approval_pending'
        assert current['captures'][0]['path'].startswith('runs/' + operation + '/')
        assert transport == [{}, {'requests': 1}]
    finally:
        worker.lease.close()


def test_failed_merge_reuses_paid_grade_and_dismissal_wins_over_running_check(tmp_path, monkeypatch):
    import candidate_checks
    root, app, row, payload = setup(tmp_path, monkeypatch)
    operation = call(app, 'POST', '/api/check-candidate', payload).json()['operation']
    original = candidate_checks.current
    calls = []
    def fail_merge(store, data):
        calls.append(True)
        if len(calls) == 2:
            raise CrawlError('Simulated interruption before merge')
        return original(store, data)
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client:
            client.return_value.request.return_value = response()
            monkeypatch.setattr(candidate_checks, 'current', fail_merge)
            worker.operation()
            assert client.return_value.request.call_count == 1
            monkeypatch.setattr(candidate_checks, 'current', original)
            assert call(app, 'POST', '/api/check-candidate', payload).json()['operation'] == operation
            worker.operation()
            assert client.return_value.request.call_count == 1
        # Replayed completion neither grades again nor creates capture work.
        with connect(root) as store:
            assert store.candidates()[0]['state'] == 'approval_pending'
            assert store.db.execute("SELECT COUNT(*) FROM operations WHERE kind!='candidate_check'").fetchone()[0] == 0
    finally:
        worker.lease.close()


def test_dismissal_during_paid_response_is_not_overwritten(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch)
    call(app, 'POST', '/api/check-candidate', payload)
    def paid(_):
        assert call(app, 'POST', '/api/decisions', [{'id': row['id'], 'manifest_sha256': row['manifest_sha256'], 'decision': 'reject'}]).status_code == 200
        return response()
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client:
            client.return_value.request.side_effect = paid
            worker.operation()
        current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
        assert current['state'] == 'rejected' and current['rating'] is None
        assert current['candidate_check']['state'] == 'interrupted'
    finally:
        worker.lease.close()


def test_unverified_coverage_and_stale_inputs_never_trigger_downloads_or_payment(tmp_path, monkeypatch):
    from site_inventory import SiteInventory
    root, app, row, payload = setup(tmp_path, monkeypatch)
    for bad in ({}, {**payload, 'max_usd': True}, {**payload, 'max_usd': 3}, {**payload, 'manifest_sha256': 'stale'}):
        assert call(app, 'POST', '/api/check-candidate', bad).status_code == 409
    assert call(app, 'POST', '/api/check-candidate', payload, peer='203.0.113.1').status_code == 403
    operation = call(app, 'POST', '/api/check-candidate', payload).json()['operation']
    monkeypatch.setattr(SiteInventory, 'check', lambda *args, **kwargs: {'status': 'inventory_partial', 'complete': False})
    worker = Worker(root)
    try:
        with patch('candidate_checks.sample') as download, patch('candidate_checks.grade') as paid:
            worker.operation()
            download.assert_not_called(); paid.assert_not_called()
        with connect(root) as store:
            assert unpack(store.db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone())['state'] == 'interrupted'
    finally:
        worker.lease.close()


def test_rejected_paid_responses_retain_reservations_on_retry(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch)
    operation = call(app, 'POST', '/api/check-candidate', payload).json()['operation']
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client:
            client.return_value.request.side_effect = CrawlError('Luna request outcome uncertain')
            for _ in range(2):
                worker.operation()
                current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
                assert current['state'] == 'grade_error'
                assert call(app, 'POST', '/api/check-candidate', {**payload, 'manifest_sha256': current['manifest_sha256']}).json()['operation'] == operation
        with connect(root / 'runs' / operation) as store:
            assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 2
            assert store.get('luna_budget_usd') == .25
            store.db.execute('UPDATE attempts SET reserved=.125')
            store.db.commit()
        with patch('grading.Luna') as client:
            worker.operation()
            client.return_value.request.assert_not_called()
        current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
        assert 'budget reached' in current['candidate_check']['error']
        assert current['candidate_check']['max_usd'] == .25
    finally:
        worker.lease.close()


def test_changed_sources_stop_evidence_recovery_before_any_paid_request(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch)
    call(app, 'POST', '/api/check-candidate', payload)
    path = root / row['captures'][0]['path']
    path.write_bytes(b'x' * path.stat().st_size)
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client, patch('candidate_checks.sample') as download:
            worker.operation()
            client.assert_not_called(); download.assert_not_called()
        current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
        assert current['rating'] is None and current['candidate_check']['state'] == 'interrupted'
        assert 'Source changed' in current['candidate_check']['error']
    finally:
        worker.lease.close()


def test_changed_scope_can_retry_cached_grade_with_original_budget(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch)
    with connect(root) as store:
        store.db.execute("UPDATE candidates SET state='approval_pending'")
        store.db.commit()
    operation = call(app, 'POST', '/api/check-candidate', payload).json()['operation']
    def paid(_):
        result = call(app, 'POST', '/api/scope', {'id': row['id'], 'manifest_sha256': row['manifest_sha256'],
                                               'mode': 'custom', 'path': '/research/'})
        assert result.status_code == 200
        return response()
    worker = Worker(root)
    try:
        with patch('grading.Luna') as client:
            client.return_value.request.side_effect = paid
            worker.operation()
            current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
            assert current['candidate_check']['state'] == 'interrupted' and current['rating'] is None
            retry = call(app, 'POST', '/api/check-candidate', {**payload, 'manifest_sha256': current['manifest_sha256'], 'max_usd': 2})
            assert retry.json()['operation'] == operation and retry.json()['max_usd'] == .25
            worker.operation()
            assert client.return_value.request.call_count == 1
        current = call(app, 'GET', '/api/candidate?id=' + row['id']).json()['candidate']
        assert current['scope'] == 'http://guild.example/research/' and current['rating']['grade'] == 3
        assert current['candidate_check']['state'] == 'completed'
    finally:
        worker.lease.close()


def test_evidence_retry_uses_current_scope_and_keeps_transport_ledger(tmp_path, monkeypatch):
    root, app, row, payload = setup(tmp_path, monkeypatch, samples=False)
    operation = call(app, 'POST', '/api/check-candidate', payload).json()['operation']
    seen = []
    def sample(args, store):
        candidate = store.candidates()[0]
        seen.append((candidate['scope'], json.loads(candidate['coverage']).get('scope_mode', 'directory'), store.get('wayback_transport')))
        store.set('wayback_transport', {'requests': 17})
        store.db.execute("UPDATE candidates SET state='sample_error',error='Archived redirect is outside the saved capture scope'")
        store.db.commit()
    worker = Worker(root)
    try:
        with patch('candidate_checks.sample', side_effect=sample), patch('grading.Luna') as luna:
            worker.operation()
            current = call(app, 'GET', '/api/candidate?id='+row['id']).json()['candidate']
            assert call(app, 'POST', '/api/scope', {'id': row['id'], 'manifest_sha256': current['manifest_sha256'], 'mode': 'site'}).status_code == 200
            current = call(app, 'GET', '/api/candidate?id='+row['id']).json()['candidate']
            assert call(app, 'POST', '/api/check-candidate', {**payload, 'manifest_sha256': current['manifest_sha256']}).json()['operation'] == operation
            worker.operation()
            luna.assert_not_called()
        assert seen == [('http://guild.example/eq/', 'directory', None), ('http://guild.example/', 'site', {'requests': 17})]
    finally:
        worker.lease.close()


def test_page_only_approval_cannot_exclude_verified_redirect_source(candidate):
    from review import record
    root, row = candidate
    with connect(root) as store:
        store.db.execute('UPDATE candidates SET url=?,scope=?,coverage=? WHERE id=?',
                         ('http://guild.example/', 'http://guild.example/', json.dumps({'status':'absent_host','scope_mode':'page'}), row['id']))
        store.db.commit()
        current = record(store, store.candidates()[0])
    app = create_app(root, start_worker=False)
    assert current['scope_has_source'] is False
    result = call(app, 'POST', '/api/decisions', [{'id': row['id'], 'manifest_sha256': current['manifest_sha256'], 'decision':'approve'}])
    assert result.status_code == 409 and 'excludes its graded source' in result.json()['error']
