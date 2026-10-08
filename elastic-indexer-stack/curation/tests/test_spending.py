from datetime import datetime, timezone
import sqlite3
import shutil

import pytest

from common import Store
from spending import Spending
from server import create_app
from test_server import call


NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def ledger(path, entries):
    store = Store(path)
    for created, actual, reserved in entries:
        store.db.execute('INSERT INTO attempts(candidate,signature,reserved,actual,status,created) VALUES (?,?,?,?,?,?)',
                         ('private-site', 'private-signature', reserved, actual, 'received' if actual is not None else 'reserved', created))
    store.db.commit()
    return store


def test_totals_include_pilot_all_runs_and_site_enrichment_without_double_counting(tmp_path):
    root = tmp_path / 'state'
    pilot = ledger(root, [('2026-09-30T23:59:59+00:00', 99, 100),
                          ('2026-10-01T00:00:00+00:00', .12, .5)])
    pilot.close()
    run = root / 'runs' / ('a' * 32)
    ledger(run, [('2026-10-08T00:00:00+00:00', .02, .1),
                 ('2026-10-08T01:00:00+00:00', None, .07)]).close()
    (run / 'initialized').touch()
    ledger(root / 'enrichment' / ('b' * 32), [
        ('2026-10-07T23:00:00+00:00', .03, .1),
        ('2026-10-08T04:00:00+00:00', .01, .1),  # rejected response
        ('2026-10-08T04:01:00+00:00', .02, .1),  # correction
        ('2026-10-08T04:02:00+00:00', None, .04), # lost correction
    ]).close()
    ledger(root / 'runs' / ('c' * 32), [('2026-10-08T00:00:00+00:00', 99, 100)]).close()
    # Both the copied-but-uninitialized campaign and arbitrary source folders
    # are excluded. No page contents or model responses enter this report.
    ledger(root / 'captures' / ('d' * 32), [('2026-10-08T00:00:00+00:00', 99, 100)]).close()
    result = Spending(root).snapshot(NOW)
    assert result['complete'] and result['currency'] == 'USD' and result['timezone'] == 'UTC'
    assert result['today'] == pytest.approx({'estimated_usd': .05, 'unresolved_usd': .11})
    assert result['month'] == pytest.approx({'estimated_usd': .20, 'unresolved_usd': .11})
    assert 'private' not in str(result) and 'signature' not in str(result)


def test_live_wal_updates_replace_reservations_and_cached_reads_do_not_charge(tmp_path, monkeypatch):
    store = ledger(tmp_path / 'state', [('2026-10-08T00:00:00+00:00', None, .2)])
    totals = Spending(store.root)
    try:
        initial = totals.snapshot(NOW)
        assert initial['today']['unresolved_usd'] == .2
        store.db.execute('UPDATE attempts SET actual=.03')
        store.db.commit()
        totals.expires = 0
        updated = totals.snapshot(NOW)
        assert updated['today'] == {'estimated_usd': .03, 'unresolved_usd': 0}
        monkeypatch.setattr(totals, 'read', lambda *args: pytest.fail('Unchanged ledger was read again'))
        totals.expires = 0
        assert totals.snapshot(NOW)['month'] == updated['month']
        assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1
    finally:
        store.close()


def test_utc_midnight_and_month_rollover_invalidate_cache(tmp_path):
    ledger(tmp_path, [('2026-10-31T23:59:59+00:00', .12, .2),
                      ('2026-11-01T00:30:00+01:00', .02, .1)]).close()
    totals = Spending(tmp_path)
    before = totals.snapshot(datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc))
    assert before['today']['estimated_usd'] == pytest.approx(.14)
    after = totals.snapshot(datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert after['today']['estimated_usd'] == after['month']['estimated_usd'] == 0


def test_unreadable_ledger_reports_incomplete_instead_of_false_zero(tmp_path):
    ledger(tmp_path, []).close()
    broken = tmp_path / 'enrichment' / ('e' * 32)
    broken.mkdir(parents=True)
    (broken / 'crawl.sqlite3').write_bytes(b'invalid sqlite')
    totals = Spending(tmp_path)
    assert totals.snapshot(NOW)['complete'] is False
    (broken / 'crawl.sqlite3').unlink()
    totals.expires = 0
    assert totals.snapshot(NOW)['complete'] is True


def test_read_only_no_database_creation_and_bounded_query(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        Spending.read(tmp_path / 'missing.sqlite3', NOW, NOW, NOW, float('inf'))
    assert not (tmp_path / 'missing.sqlite3').exists()
    ledger(tmp_path, [('2026-10-08T00:00:00+00:00', .01, .2)] * 1000).close()
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        Spending.read(tmp_path / 'crawl.sqlite3', NOW, NOW.replace(day=1), NOW, 0)


def test_queue_includes_safe_spend_independent_of_view_and_inventory_caps(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    for view in ('candidates', 'history', 'indexing'):
        response = call(app, 'GET', '/api/queue?filter=' + view)
        assert response.status_code == 200
        assert response.json()['luna_spend']['complete'] is True
        assert response.json()['luna_spend']['today']['estimated_usd'] == 0
        assert response.headers['cache-control'] == 'no-store'


def test_every_site_ledger_is_counted_beyond_the_batch_inventory_cap(tmp_path):
    root = tmp_path / 'state'
    ledger(root, []).close()
    template = tmp_path / 'template'
    ledger(template, [('2026-10-08T00:00:00+00:00', .001, .1)]).close()
    for index in range(105):
        folder = root / 'enrichment' / f'{index:032x}'
        folder.mkdir(parents=True)
        shutil.copyfile(template / 'crawl.sqlite3', folder / 'crawl.sqlite3')
    totals = Spending(root).snapshot(NOW)
    assert totals['complete']
    assert totals['today']['estimated_usd'] == pytest.approx(.105)
