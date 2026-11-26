"""Per-operation ceilings use the same settled/pending semantics as UTC caps."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3

import pytest

from common import CrawlError, Store
from daily_budget import (SiteBudgetExceeded, attempt_remaining_units, attempt_spending,
                          install_scope_totals, reserve_attempt, scope_spending_units, units)
from grading import PRICING, reserve


def test_settlement_counts_rejected_responses_corrections_and_unknown_usage(tmp_path):
    with closing(Store(tmp_path / 'operation')) as store:
        store.db.executemany('INSERT INTO attempts(reserved,actual,status) VALUES (?,?,?)', [
            (1.0, .1, 'received'),  # Rejected metadata still costs money.
            (.5, .02, 'enriched'),  # Its correction has its own settled cost.
            (.3, None, 'received'),  # Response without usage is not free.
            (.2, None, 'reserved'),  # Ambiguous/lost response.
            (.4, 0, 'enriched'),  # Verified zero, rather than missing usage.
        ])
        store.db.commit()
        assert attempt_spending(store) == {'estimated_usd': .12, 'reserved_usd': .5}
        assert attempt_remaining_units(store, 2) == units(1.38)
        assert store.db.execute('SELECT SUM(reserved) FROM attempts').fetchone()[0] == 2.4


def test_parallel_admission_cannot_overspend_after_old_reservations_settle(tmp_path):
    root = tmp_path / 'operation'
    with closing(Store(root)) as store:
        store.db.execute("INSERT INTO attempts(reserved,actual,status) VALUES (10,.1,'enriched')")
        store.db.commit()
    payload = {'max_output_tokens': 2500}
    amount = ((len(json.dumps(payload).encode()) + 8192) * PRICING['input_per_million']
              + payload['max_output_tokens'] * PRICING['output_per_million']) / 1_000_000
    # Two requests fit at nanodollar precision, including conservative rounding.
    maximum = (units(.1) + 2 * units(amount)) / 1_000_000_000
    def request(index):
        with closing(Store(root)) as store:
            try:
                return reserve(store, str(index), str(index), payload, maximum)
            except CrawlError as error:
                assert 'budget' in str(error)
                return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = [result for result in pool.map(request, range(8)) if result is not None]
    assert len(admitted) == 2
    with closing(Store(root)) as store:
        assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 3
        assert attempt_remaining_units(store, maximum) == 0


def test_invalid_costs_do_not_make_more_budget_available(tmp_path):
    with closing(Store(tmp_path / 'operation')) as store:
        for actual in (-.1, float('inf')):
            store.db.execute('DELETE FROM attempts')
            store.db.execute('INSERT INTO attempts(reserved,actual) VALUES (.2,?)', (actual,))
            store.db.commit()
            with pytest.raises(CrawlError, match='accounting'):
                attempt_remaining_units(store, 2)


def test_scope_totals_follow_legacy_writers_and_preserve_exact_rounding(tmp_path):
    with closing(Store(tmp_path)) as store:
        store.db.execute('INSERT INTO attempts(id,reserved,actual) VALUES (1,.2,.0000000011),(2,.0000000011,NULL)')
        store.db.commit()
        install_scope_totals(store.db)
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            assert scope_spending_units(store.db) == 4
        with closing(sqlite3.connect(tmp_path / 'crawl.sqlite3')) as legacy, legacy:
            legacy.execute('UPDATE attempts SET id=3,actual=.2 WHERE id=2')
            legacy.execute('INSERT OR REPLACE INTO attempts(id,reserved,actual) VALUES (1,.03,NULL)')
            legacy.execute('INSERT INTO attempts(id,reserved,actual) VALUES (4,.6,.5)')
            legacy.execute('DELETE FROM attempts WHERE id=4')
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            assert scope_spending_units(store.db) == units(.23)
            assert scope_spending_units(store.db) == sum(units(part) for part in attempt_spending(store).values())
        assert not store.db.execute('SELECT 1 FROM scope_attempt_dirty').fetchone()


def test_scope_totals_reject_damaged_rows_and_recover_after_repair(tmp_path):
    with closing(Store(tmp_path)) as store, store.db:
        store.db.execute('INSERT INTO attempts(id,reserved,actual) VALUES (1,.02,-.01)')
        store.db.commit()
        install_scope_totals(store.db)
        store.db.execute('BEGIN IMMEDIATE')
        with pytest.raises(CrawlError, match='accounting'):
            scope_spending_units(store.db)
        store.db.rollback()
        store.db.execute('UPDATE attempts SET actual=.01 WHERE id=1')
        assert scope_spending_units(store.db) == units(.01)


def test_concurrent_scope_reservations_respect_cap_with_projected_totals(tmp_path):
    with closing(Store(tmp_path)) as store:
        store.db.execute('INSERT INTO attempts(reserved,actual) VALUES (.9,.1)')
        store.db.commit()
        install_scope_totals(store.db)

    def reserve_slot(_):
        with closing(Store(tmp_path, initialize=False)) as store:
            store.db.execute('BEGIN IMMEDIATE')
            attempt = store.db.execute('INSERT INTO attempts(reserved) VALUES (.2)').lastrowid
            try:
                reserve_attempt(store, attempt, .2, site_maximum=.5, scope_totals=True)
            except SiteBudgetExceeded:
                return False
            store.db.commit()
            return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve_slot, range(8))) == 2
    with closing(Store(tmp_path, initialize=False)) as store:
        assert sum(units(part) for part in attempt_spending(store).values()) == units(.5)
