import copy
import json
from unittest.mock import patch

import pytest

from automatic_indexing import prepare_next
from capture_budget import budget_identity
from captures import capture_sites
from common import CrawlError, digest
from full_capture import POLICY
from server import create_app
from state import Lane, connect, unpack
from test_capture_continuation import legacy, detail, regenerate
from test_capture_queue import make_due
from test_server import call
from worker import Worker


class EmptyCatalog:
    def __init__(self, *args): pass
    def call(self, job):
        assert job['op'] == 'scope_list'
        return {'captures': []}
    def close(self): pass


def completed(root, url='http://guild.example/', identifier='a' * 32, gaps=False):
    row, before = legacy(root, count=1, url=url, identifier=identifier)
    manifest = capture_sites(root, identifier, [{**before['sites'][0], 'capture_policy': POLICY}], EmptyCatalog)
    if gaps:
        manifest['capture_retry'] = {'files': 1, 'lookups': 0}
        manifest['notes'] = [{'url': row['url'] + 'missing.gif', 'reason': 'Wayback HTTP 403'}]
    (root / 'batches' / identifier / 'complete' / 'manifest.json').write_text(json.dumps(manifest))
    with connect(root) as store:
        store.db.execute('UPDATE batches SET manifest=?,manifest_sha256=? WHERE id=?',
                         (json.dumps(manifest), digest(manifest), identifier))
        store.db.commit()
    return row, manifest


def published(root, manifest, state='published_waiting_index'):
    publication = {'commit': 'c' * 40}
    job = {'name': 'retained-import', 'attempt': 1}
    path = root / 'batches' / manifest['batch_id'] / 'approved.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'manifest': manifest, 'manifest_sha256': digest(manifest), 'publication': publication}))
    with connect(root) as store:
        store.db.execute('UPDATE batches SET state=?,publication=?,job=? WHERE id=?',
                         (state, json.dumps(publication), json.dumps(job), manifest['batch_id']))
        store.db.execute('UPDATE candidates SET state=? WHERE id=?', ('indexed' if state == 'indexed' else 'published', manifest['sites'][0]['id']))
        store.db.commit()
    return path.read_bytes()


def test_finished_site_queues_once_under_original_approval_and_keeps_full_reindex(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    row, manifest = completed(root, gaps=True)
    app = create_app(root, start_worker=False)
    original_decision = detail(app, row)['candidate']['decision']
    result = prepare_next(root)
    assert result['operation']
    assert prepare_next(root) is None
    current = detail(app, row)
    assert current['candidate']['stage'] == 'indexing'
    assert current['candidate']['decision'] == original_decision
    assert current['review']['state'] == 'publication_requested'
    with connect(root) as store:
        assert store.db.execute("SELECT COUNT(*) FROM operations WHERE kind='publish'").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM events WHERE action='site_indexing_approve'").fetchone()[0] == 0
        event = json.loads(store.db.execute("SELECT detail FROM events WHERE action='site_indexing_automatic'").fetchone()[0])
        assert event['authorization'] == 'original_site_capture_approval'
    reindex = {'metadata': {'name': 'existing-reindex', 'uid': 'same'}, 'status': {}, 'spec': {}}
    class Kube:
        def jobs(self): return [reindex]
        def create(self, _): raise AssertionError('Do not interrupt pending/active full reindex')
    monkeypatch.setattr('worker.publish', lambda *args: {'commit': 'c' * 40})
    worker = Worker(root, kube=Kube())
    try:
        worker.operation(Lane.INDEXING)
        worker.indexing()
    finally:
        worker.close()
    current = detail(app, row)
    assert current['review']['state'] == 'published_waiting_index'
    assert current['review']['job']['waiting_for'] == ['existing-reindex']
    assert current['review']['manifest']['captures'] == manifest['captures']
    assert current['review']['manifest']['capture_retry']['files'] == 1
    # Restart/polling cannot create another publication or require a second approval.
    assert prepare_next(root) is None
    assert detail(create_app(root, start_worker=False), row)['candidate']['stage'] == 'indexing'


@pytest.mark.parametrize('failure', ['source', 'approval', 'scope', 'legacy', 'coverage', 'internal'])
def test_preflight_failure_is_visible_durable_and_never_publishes(tmp_path, failure):
    root = tmp_path / 'state'
    row, manifest = legacy(root) if failure == 'legacy' else completed(root)
    app = create_app(root, start_worker=False)
    with connect(root) as store:
        if failure == 'source': (root / manifest['captures'][0]['path']).write_bytes(b'tampered')
        if failure == 'approval': store.db.execute('UPDATE candidates SET decision=NULL')
        if failure == 'scope': store.db.execute("UPDATE candidates SET scope='http://guild.example/other/'")
        store.db.commit()
    with patch('automatic_indexing.require_eligible', side_effect=CrawlError('Archive coverage unverified')) if failure == 'coverage' else patch('automatic_indexing.policy', side_effect=RuntimeError('private detail')) if failure == 'internal' else patch('automatic_indexing.now', return_value='2026-10-09'):
        prepare_next(root)
    current = detail(app, row)
    assert current['candidate']['stage'] == 'indexing'
    assert current['review']['state'] == 'index_preflight_failed'
    assert current['review']['error'] and 'private detail' not in current['review']['error']
    with patch('automatic_indexing.check_manifest', side_effect=AssertionError('No implicit retry')):
        assert prepare_next(root) is None
    with connect(root) as store:
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()


def test_preflight_retry_is_explicit_hash_bound_and_preserves_manifest(tmp_path):
    root = tmp_path / 'state'
    row, manifest = completed(root)
    with patch('automatic_indexing.require_eligible', side_effect=CrawlError('Coverage needs checking')): prepare_next(root)
    app = create_app(root, start_worker=False)
    payload = {'id': manifest['batch_id'], 'manifest_sha256': digest(manifest)}
    assert call(app, 'POST', '/api/prepare-indexing', {}).status_code == 409
    assert call(app, 'POST', '/api/prepare-indexing', {**payload, 'manifest_sha256': 'stale'}).status_code == 409
    assert call(app, 'POST', '/api/prepare-indexing', payload).status_code == 202
    assert call(app, 'POST', '/api/prepare-indexing', payload).status_code == 409
    assert detail(app, row)['review']['error'] is None
    assert prepare_next(root)['operation']
    assert detail(app, row)['review']['manifest'] == manifest


def test_history_is_never_automatically_reactivated_and_old_review_filter_remains(tmp_path):
    root = tmp_path / 'state'
    first, _ = completed(root)
    second, second_manifest = completed(root, 'http://other.example/', 'b' * 32)
    app = create_app(root, start_worker=False)
    assert call(app, 'POST', '/api/site-decision', {'id': second_manifest['batch_id'], 'manifest_sha256': digest(second_manifest), 'decision': 'decline'}).status_code == 200
    legacy_list = call(app, 'GET', '/api/queue?filter=review').json()
    assert [r['id'] for r in legacy_list['candidates']] == [first['id']]
    assert 'review' not in legacy_list['stage_counts']
    assert prepare_next(root)['operation']
    assert prepare_next(root) is None
    assert detail(app, second)['candidate']['stage'] == 'history'


def test_intervening_decline_during_preflight_cannot_be_overwritten(tmp_path):
    from captures import check_manifest
    root = tmp_path / 'state'
    row, manifest = completed(root)
    app = create_app(root, start_worker=False)
    def decline(root, manifest):
        check_manifest(root, manifest)
        assert call(app, 'POST', '/api/site-decision', {'id': manifest['batch_id'], 'manifest_sha256': digest(manifest), 'decision': 'decline'}).status_code == 200
    with patch('automatic_indexing.check_manifest', side_effect=decline): prepare_next(root)
    assert detail(app, row)['candidate']['stage'] == 'history'
    with connect(root) as store: assert not store.db.execute('SELECT 1 FROM operations').fetchone()


@pytest.mark.parametrize('state', ['published_waiting_index', 'indexing', 'index_failed', 'indexed'])
def test_published_gaps_can_queue_and_undo_without_altering_original_jobs(tmp_path, state):
    root = tmp_path / 'state'
    row, manifest = completed(root, gaps=True)
    original = published(root, manifest, state)
    app = create_app(root, start_worker=False)
    before = detail(app, row)['review']
    assert regenerate(app, manifest).status_code == 202
    assert detail(app, row)['candidate']['stage'] == 'queued'
    make_due(root)
    assert call(app, 'POST', '/api/undo', {'id': row['id'], 'manifest_sha256': row['manifest_sha256']}).status_code == 200
    after = detail(app, row)['review']
    assert after['state'] == before['state'] and after['job'] == before['job']
    assert (root / 'batches' / manifest['batch_id'] / 'approved.json').read_bytes() == original
    assert detail(app, row)['candidate']['stage'] == ('history' if state == 'indexed' else 'indexing')


def test_published_retry_keeps_sources_jobs_and_original_budget_across_recapture(tmp_path, monkeypatch):
    from capture_continuation import queued_site, require_eligible
    from review import record
    root = tmp_path / 'state'
    row, manifest = completed(root, gaps=True)
    original = published(root, manifest)
    app = create_app(root, start_worker=False)
    assert regenerate(app, manifest).status_code == 202
    with connect(root) as store:
        current = record(store, store.db.execute('SELECT * FROM candidates WHERE id=?', (row['id'],)).fetchone())
        site = queued_site(store, current)
        # Only the exact saved predecessor exempts this site from duplicate rejection.
        require_eligible(store, site)
        with pytest.raises(CrawlError): require_eligible(store, {**site, 'scope': 'http://unrelated.example/'})
    worker = Worker(root)
    monkeypatch.setattr('worker.capture_sites', lambda root, batch, sites, progress: capture_sites(root, batch, sites, EmptyCatalog, progress))
    try:
        make_due(root); worker.capture_queue(); worker.operation()
        current = detail(app, row)
        assert current['candidate']['stage'] == 'indexing', current['capture_operation']
        retry = current['review']['manifest']
        assert retry['captures'] == manifest['captures']
        assert retry['enrichment_budget_id'] == manifest['batch_id']
        assert budget_identity(root, retry) == manifest['batch_id']
        assert prepare_next(root)['operation']
    finally: worker.close()
    with connect(root) as store:
        before = unpack(store.db.execute('SELECT * FROM batches WHERE id=?', (manifest['batch_id'],)).fetchone())
        assert before['state'] == 'published_waiting_index' and before['job']['name'] == 'retained-import'
    assert (root / 'batches' / manifest['batch_id'] / 'approved.json').read_bytes() == original


@pytest.mark.parametrize('change', ['budget', 'scope', 'hash', 'missing', 'unpublished', 'invalid_id', 'omitted_budget'])
def test_retry_cannot_reset_budget_or_borrow_another_site_budget(tmp_path, change):
    root = tmp_path / 'state'
    _, manifest = completed(root, gaps=True)
    published(root, manifest)
    prior = {'review_id': manifest['batch_id'], 'manifest_sha256': digest(manifest), 'published': True, 'mode': 'retry_failed'}
    retry = {**copy.deepcopy(manifest), 'batch_id': 'b' * 32, 'enrichment_budget_id': manifest['batch_id']}
    retry['sites'][0]['continued_from'] = prior
    assert budget_identity(root, retry) == manifest['batch_id']
    if change == 'budget': retry['enrichment_budget_id'] = 'c' * 32
    if change == 'scope': retry['sites'][0]['scope'] = 'http://other.example/'
    if change == 'hash': prior['manifest_sha256'] = 'd' * 64
    if change == 'missing': (root / 'batches' / manifest['batch_id'] / 'approved.json').unlink()
    if change == 'unpublished': prior['published'] = False
    if change == 'invalid_id': retry['enrichment_budget_id'] = '../escape'
    if change == 'omitted_budget': del retry['enrichment_budget_id']
    with pytest.raises(CrawlError): budget_identity(root, retry)


def test_earlier_import_completion_does_not_retire_queued_retry_and_undo_restores_completion(tmp_path):
    from jobs import import_name
    root = tmp_path / 'state'
    row, manifest = completed(root, gaps=True)
    published(root, manifest)
    app = create_app(root, start_worker=False)
    assert regenerate(app, manifest).status_code == 202
    with connect(root) as store:
        batch = unpack(store.db.execute('SELECT * FROM batches').fetchone())
    job = {'metadata': {'name': import_name(batch), 'uid': 'retained',
                        'annotations': {'eqarchives.org/manifest-sha256': digest(manifest)}},
           'status': {'conditions': [{'type': 'Complete', 'status': 'True'}]}}
    class Kube:
        def jobs(self): return [job]
        def create(self, _): raise AssertionError('Existing completed Job is retained')
    worker = Worker(root, kube=Kube())
    try: worker.indexing()
    finally: worker.close()
    assert detail(app, row)['candidate']['stage'] == 'queued'
    assert detail(app, row)['review']['state'] == 'indexed'
    assert call(app, 'POST', '/api/undo', {'id': row['id'], 'manifest_sha256': row['manifest_sha256']}).status_code == 200
    assert detail(app, row)['candidate']['stage'] == 'history'
    assert job['metadata']['uid'] == 'retained'


def test_import_retry_uses_original_paid_ledger_and_current_job_diagnostics(tmp_path, monkeypatch):
    import index_captures
    from import_status import EnrichmentError, status_path
    root = tmp_path / 'state'
    _, manifest = completed(root, gaps=True)
    published(root, manifest)
    retry = {**copy.deepcopy(manifest), 'batch_id': 'b' * 32, 'enrichment_budget_id': manifest['batch_id']}
    retry['sites'][0]['continued_from'] = {'review_id': manifest['batch_id'], 'manifest_sha256': digest(manifest), 'published': True, 'mode': 'retry_failed'}
    batch = {'manifest': retry, 'manifest_sha256': digest(retry), 'publication': {'commit': 'c' * 40}}
    job = 'eqarchives-captures-' + retry['batch_id']
    directories = []
    class Enricher:
        def __init__(self, directory, key, maximum):
            directories.append(directory)
            assert maximum == 2
        def close(self): pass
    monkeypatch.setattr(index_captures, 'Enricher', Enricher)
    monkeypatch.setattr(index_captures, 'Services', lambda **kwargs: kwargs['enricher'])
    monkeypatch.setattr(index_captures, 'read_batch', lambda *args: batch)
    def fail(*args): raise EnrichmentError('budget')
    monkeypatch.setattr(index_captures, 'index_batch', fail)
    monkeypatch.setenv('IMPORT_JOB_NAME', job)
    monkeypatch.setattr('sys.argv', ['index_captures', '--root', str(root), '--batch', 'approved.json',
                        '--manifest-sha256', digest(retry), '--enrichment-root', str(root / 'enrichment')])
    assert index_captures.main() == 1
    assert directories == [root / 'enrichment' / manifest['batch_id']]
    status = json.loads(status_path(root / 'enrichment' / retry['batch_id'], job).read_text())
    assert status['state'] == 'failed' and status['manifest_sha256'] == digest(retry)
    assert not status_path(root / 'enrichment' / manifest['batch_id'], job).exists()


def test_preparation_resumes_partial_coverage_metadata_without_archive_walks(tmp_path, monkeypatch):
    from discovery import Archive
    from site_inventory import SiteInventory
    from test_coverage_check import archive
    root = tmp_path / 'state'
    row, manifest = completed(root, url='http://geocities.com/bob/')
    repo = archive(tmp_path, ['geocities.com/19991201000000/alice/eq/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO', str(repo))
    app = create_app(root, start_worker=False)
    with connect(root) as store:
        reader = Archive(repo, store)
        # Populate the independent coverage cache with a real resumable pause.
        cache = SiteInventory(reader, max_trees=0)
        result = cache.check(row['url'], force=True)
        assert result['status'] == 'inventory_partial', result
    prepare_next(root)
    current = detail(app, row)
    assert current['review']['state'] == 'publication_requested', current['review']['error']


def test_published_file_retry_survives_coverage_refresh_and_still_blocks_alias_duplicates(tmp_path, monkeypatch):
    from coverage_check import require_new
    from test_coverage_check import archive
    root = tmp_path / 'state'
    row, manifest = completed(root, gaps=True)
    published(root, manifest)
    repo = archive(tmp_path, ['guild.example/20000101000000/index.html'])
    monkeypatch.setenv('ARCHIVE_REPO', str(repo))
    app = create_app(root, start_worker=False)
    assert regenerate(app, manifest).status_code == 202
    make_due(root)
    worker = Worker(root)
    try:
        worker.capture_queue()
        assert detail(app, row)['candidate']['stage'] == 'capturing'
    finally: worker.close()
    # Even a stale archive snapshot must remember this site's published batch
    # while its candidate is temporarily back in the capture pipeline.
    with patch('coverage_check.SiteInventory') as inventory:
        inventory.return_value.check.return_value = {'status': 'new_site'}
        with connect(root) as store, pytest.raises(CrawlError, match='already published'):
            require_new(store, 'https://www.guild.example/')
