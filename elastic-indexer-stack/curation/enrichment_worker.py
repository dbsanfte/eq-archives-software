"""Run the independent, bounded historical enrichment queue."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from contextvars import copy_context
from datetime import datetime
import fcntl
from itertools import cycle
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from common import CrawlError, Store, digest, save
from daily_budget import AutomaticStopped, DailyBudget, DailyBudgetPause, EnrichmentPriorityPause, install_scope_totals
from grading import Luna, LunaUnavailable
from import_status import EnrichmentError
from indexer import gemma, passages, search_enrichment
from indexer.capture_enrichment import Enricher, summary_requests
from indexer.sqlite_state import SQLiteState
from policy_enrichment import COLLECTIONS, COUNTERS, capture_identity, metadata_patch, prepare_refresh
from policy_maintenance import MaintenanceServices, YieldWork, foreground, identity
from policy_sources import ServiceError, SourceError, Sources


WORKERS = 8
MAX_WORKERS = 32
PAGE_SIZE = 64
MAX_RECORDS = 1200
SLICE_SECONDS = 720
LUNA_SECONDS = 45
MIN_REQUEST_TIME_LEFT = 15
SUMMARY_BATCH_SIZE = 16
SUMMARY_BATCH_WAIT = 1
EXECUTION_COUNTERS = ('embedding_waves', 'examined', 'checkpoint_views', 'rebuilt_views')


class State:
    def __init__(self, root, key):
        self.root, self.key = Path(root), key + ':' + search_enrichment.VERSION
        self.root.mkdir(parents=True, exist_ok=True, mode=0o770)
        fd = os.open(self.root / 'enrichment-maintenance.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
        self.lease = os.fdopen(fd, 'a')
        self.db = SQLiteState(self.root / 'enrichment-maintenance.sqlite', durable=True)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.db.transaction(attempts=6) as db:
                db.execute('''CREATE TABLE IF NOT EXISTS progress (
                    key TEXT PRIMARY KEY, updated INTEGER, reused INTEGER, failed INTEGER, skipped INTEGER, reason TEXT)''')
                db.execute('''CREATE TABLE IF NOT EXISTS frontiers (
                    key TEXT, collection TEXT, cursor TEXT, next_at REAL, PRIMARY KEY(key,collection))''')
                db.execute('''CREATE TABLE IF NOT EXISTS pending (
                    key TEXT, collection TEXT, id TEXT, attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0,
                    PRIMARY KEY(key,id))''')
                if not db.execute('SELECT 1 FROM progress WHERE key=?', (self.key,)).fetchone():
                    self.migrate(db)
        except BaseException:
            self.close()
            raise

    def migrate(self, db):
        """Keep the old lane's counters and frontier after its final paid slice."""
        old = self.root / 'policy-maintenance.sqlite'
        cursor, counts, reason = '', (0, 0, 0, 0), None
        if old.exists():
            with closing(sqlite3.connect(old.as_uri() + '?mode=ro', uri=True)) as previous:
                if previous.execute("SELECT 1 FROM sqlite_master WHERE name='policy_enrichment'").fetchone():
                    row = previous.execute('SELECT cursor,updated,reused,failed,skipped,last_reason FROM policy_enrichment WHERE key=?',
                                           (self.key,)).fetchone()
                    if row:
                        cursor, *counts, reason = row
        db.execute('INSERT INTO progress VALUES (?,?,?,?,?,?)', (self.key, *counts, reason))
        db.executemany('INSERT INTO frontiers VALUES (?,?,?,0)', [
            (self.key, collection, cursor if cursor.startswith(collection + '/') else '') for collection in COLLECTIONS])

    def close(self):
        self.db.close()
        self.lease.close()

    def fill(self, services, check):
        now = time.time()
        db = self.db.connection()
        for collection, cursor, next_at in db.execute('SELECT collection,cursor,next_at FROM frontiers WHERE key=?', (self.key,)).fetchall():
            if next_at > now:
                continue
            queued, = db.execute('SELECT COUNT(*) FROM pending WHERE key=? AND collection=?', (self.key, collection)).fetchone()
            if queued > PAGE_SIZE // 2:
                continue
            check()
            identifiers = services.select_enrichment(cursor, collection=collection, limit=PAGE_SIZE - queued)
            if any(not identifier.startswith(collection + '/') for identifier in identifiers):
                raise ServiceError('response_identity')
            with self.db.transaction(attempts=6) as db:
                # The cursor can only pass IDs once they are in the durable queue.
                db.executemany('INSERT OR IGNORE INTO pending(key,collection,id) VALUES (?,?,?)',
                               [(self.key, collection, identifier) for identifier in identifiers])
                db.execute('UPDATE frontiers SET cursor=?,next_at=? WHERE key=? AND collection=?',
                           (identifiers[-1] if identifiers else '', 0 if identifiers else now + 900, self.key, collection))

    def ready(self, rotation, limit, active=frozenset()):
        rows = {name: [] for name in COLLECTIONS}
        for collection, identifier in self.db.connection().execute(
                'SELECT collection,id FROM pending WHERE key=? AND retry_at<=? ORDER BY id', (self.key, time.time())):
            if identifier not in active:
                rows[collection].append(identifier)
        result = []
        while len(result) < limit and any(rows.values()):
            collection = next(rotation)
            if rows[collection]:
                result.append(rows[collection].pop(0))
        return result

    def next_at(self):
        row = self.db.connection().execute('''SELECT MIN(ready) FROM (
            SELECT retry_at AS ready FROM pending WHERE key=? UNION ALL
            SELECT next_at FROM frontiers f WHERE f.key=? AND NOT EXISTS (
                SELECT 1 FROM pending p WHERE p.key=f.key AND p.collection=f.collection))''', (self.key, self.key)).fetchone()
        return row[0] or 0

    def begin(self, identifier):
        with self.db.transaction(attempts=6) as db:
            attempts, = db.execute('UPDATE pending SET attempts=attempts+1 WHERE key=? AND id=? RETURNING attempts',
                                   (self.key, identifier)).fetchone()
        return attempts <= 3

    def release(self, identifiers):
        # Budget, foreground and transport yields do not consume crash recovery attempts.
        with self.db.transaction(attempts=6) as db:
            db.executemany('UPDATE pending SET attempts=MAX(0,attempts-1) WHERE key=? AND id=?',
                           [(self.key, identifier) for identifier in identifiers])

    def defer(self, identifier):
        with self.db.transaction(attempts=6) as db:
            db.execute('UPDATE pending SET retry_at=? WHERE key=? AND id=?',
                       (time.time() + 30, self.key, identifier))

    def finish(self, identifier, result, reason=None):
        if result not in COUNTERS:
            raise ValueError('Unknown re-enrichment result')
        with self.db.transaction(attempts=6) as db:
            db.execute('DELETE FROM pending WHERE key=? AND id=?', (self.key, identifier))
            db.execute(f'UPDATE progress SET {result}={result}+1,reason=COALESCE(?,reason) WHERE key=?',
                       (reason, self.key))

    def counts(self):
        return dict(zip((*COUNTERS, 'last_reason'), self.db.connection().execute(
            'SELECT updated,reused,failed,skipped,reason FROM progress WHERE key=?', (self.key,)).fetchone(), strict=True))


def write_wave(services, records, check):
    """Validate the whole embedding batch before acknowledging any writes."""
    requests = {}
    for prepared, patch in records:
        if patch is not None:
            inputs = summary_requests(patch['llm_summary'])
            if not inputs:
                raise ServiceError('summary_embedding')
            requests[prepared.hit['_id']] = inputs
    unique = list(dict.fromkeys(request for inputs in requests.values() for request in inputs))
    vectors = services.vectors(unique, check) if unique else []
    try:
        by_input = dict(zip(unique, vectors, strict=True))
        summaries = {key: gemma.mean_vector([by_input[request] for request in inputs]) for key, inputs in requests.items()}
    except (TypeError, ValueError, KeyError):
        raise ServiceError('summary_embedding') from None
    checkpoint = {'llm_enrichment_passage_version': passages.VERSION}
    for prepared, patch in records:
        check()
        identifier = prepared.hit['_id']
        update = checkpoint if patch is None else {**patch, **checkpoint, 'llm_summary_vector': summaries[identifier]}
        try:
            services.update(prepared.hit, update)
        except SourceError as error:
            yield identifier, error.reason
        else:
            yield identifier, 'reused' if patch is None else 'updated'


def run(root, state, services, sources, job, account, *, seconds=SLICE_SECONDS, maximum=MAX_RECORDS, workers=WORKERS,
        enricher_factory=Enricher, should_yield=None):
    if not 0 < seconds <= SLICE_SECONDS or not 1 <= maximum <= MAX_RECORDS or not 1 <= workers <= MAX_WORKERS:
        raise ValueError('Invalid historical enrichment slice bounds')
    deadline = time.monotonic() + seconds
    should_yield = should_yield or (lambda: foreground(root, enrichment=True))
    phase, next_at = 'bounded', 0
    counters = {'luna_workers': workers, **dict.fromkeys(EXECUTION_COUNTERS, 0)}
    rotation, capped = cycle(COLLECTIONS), set()
    started, initialized = set(), set()
    futures, scheduled = {}, {}
    outcomes = []
    buffered_at = 0

    def check():
        if time.monotonic() >= deadline or should_yield():
            raise YieldWork()

    def report(terminal=False):
        counts = state.counts()
        record = {'job': job, 'policy': passages.VERSION, 'model': gemma.MODEL, 'key': identity(),
                  'image': os.environ.get('IMPORT_IMAGE', ''), 'updated_at': time.time(), 'terminal': terminal,
                  **{k: counts[k] for k in COUNTERS}, 'phase': phase, 'next_at': next_at,
                  'scanned': sum(counts[k] for k in COUNTERS), 'unresolved': 0, 'execution': counters,
                  'enrichment': {**counts, 'phase': phase, 'next_at': next_at}}
        save(state.root / (job + '.json'), record)
        return record

    def grade(prepared, cache):
        def check_request():
            check()
            if deadline - time.monotonic() < MIN_REQUEST_TIME_LEFT:
                raise YieldWork()

        def client(path):
            return Luna(path, deadline=time.time() + LUNA_SECONDS)
        with closing(enricher_factory(cache, '/run/secrets/luna_api_key', client_factory=client,
                                     check=check_request, prepared_cache=True)) as enricher:
            return metadata_patch(enricher.enrich_for_maintenance(prepared.capture, prepared.source, prepared.grounding))

    def finish(identifier, result, reason=None):
        if result == 'concurrent_update':
            state.defer(identifier)
        elif result not in COUNTERS:
            state.finish(identifier, 'failed', result)
        else:
            state.finish(identifier, result, reason)
        started.discard(identifier)

    def completed(prepared, patch):
        nonlocal buffered_at
        if not outcomes:
            buffered_at = time.monotonic()
        outcomes.append((prepared, patch))

    def admit(pool, limit):
        state.fill(services, check)
        identifiers = state.ready(rotation, limit, started)
        for identifier in identifiers:
            check()
            counters['examined'] += 1
            if not state.begin(identifier):
                finish(identifier, 'failed', 'repeated_worker_interruption')
                continue
            started.add(identifier)
            try:
                url, scope = capture_identity(identifier)
                prepared = None if scope in capped else prepare_refresh(services, sources, identifier, url, check)
            except SourceError as error:
                finish(identifier, error.reason)
                continue
            except ServiceError:
                raise
            except (TypeError, KeyError, ValueError):
                finish(identifier, 'failed', 'source_validation')
                continue
            if prepared is None:
                finish(identifier, 'skipped')
                continue
            counters['checkpoint_views' if prepared.checkpoint_view else 'rebuilt_views'] += 1
            if prepared.capture is None:
                completed(prepared, None)
                continue
            cache = state.root / 'policy-enrichment' / digest(scope)
            if scope not in initialized:
                with closing(Store(cache)) as store:
                    install_scope_totals(store.db)
                initialized.add(scope)
            key = scope, prepared.capture['url'], prepared.capture['sha256'], digest(prepared.grounding)
            if key not in scheduled:
                future = pool.submit(copy_context().run, grade, prepared, cache)
                scheduled[key] = future
                futures[future] = (key, [])
            futures[scheduled[key]][1].append(prepared)
        return bool(identifiers)

    def collect(pause):
        for future in tuple(futures):
            if not future.done():
                continue
            key, records = futures.pop(future)
            del scheduled[key]
            try:
                patch = future.result()
            except EnrichmentError as error:
                if error.code == 'budget':
                    capped.add(key[0])
                for prepared in records:
                    finish(prepared.hit['_id'], 'skipped' if error.code == 'budget' else 'failed',
                           'enrichment_' + error.code)
            except Exception as error:
                pause = pause or error
            else:
                for prepared in records:
                    completed(prepared, patch)
        return pause

    def flush():
        counters['embedding_waves'] += int(any(patch is not None for _, patch in outcomes))
        for identifier, result in write_wave(services, outcomes, check):
            finish(identifier, result)
        outcomes.clear()

    try:
        budget = account.snapshot(maintenance=True)
        if not budget['complete']:
            phase, next_at = 'waiting_configuration', time.time() + 300
            return report(True)
        if budget['remaining_usd'] <= 0:
            phase, next_at = 'waiting_budget', datetime.fromisoformat(budget['resets_at']).timestamp()
            return report(True)
        if budget['enrichment_pending']:
            phase, next_at = 'waiting_imports', time.time() + 60
            return report(True)
        # The pool drains before its shared priority lease is released, even
        # when the slice yields or encounters a failed write.
        with account.bind(purpose='maintenance', owner=job), ThreadPoolExecutor(max_workers=workers) as pool:
            last_report = 0
            pause = None
            while True:
                pause = collect(pause)
                now = time.monotonic()
                if outcomes and (len(outcomes) >= SUMMARY_BATCH_SIZE or not futures or pause
                                 or now - buffered_at >= SUMMARY_BATCH_WAIT):
                    flush()
                if now - last_report >= 5:
                    report()
                    last_report = now
                if not pause:
                    check()
                    in_flight = sum(len(records) for _, records in futures.values())
                    capacity = min(workers - in_flight, maximum - counters['examined'])
                    if capacity > 0 and admit(pool, capacity):
                        continue
                if not futures:
                    if pause:
                        raise pause
                    if counters['examined'] < maximum:
                        phase, next_at = 'sweep_waiting', max(time.time() + 10, state.next_at())
                    break
                wait_time = max(0, buffered_at + SUMMARY_BATCH_WAIT - now) if outcomes else SUMMARY_BATCH_WAIT
                wait(futures, timeout=wait_time, return_when=FIRST_COMPLETED)
    except YieldWork:
        phase = 'yielded'
    except DailyBudgetPause as error:
        phase, next_at = 'waiting_budget', datetime.fromisoformat(error.retry_at).timestamp()
    except EnrichmentPriorityPause:
        phase, next_at = 'waiting_imports', time.time() + 60
    except AutomaticStopped:
        phase, next_at = 'waiting_configuration', time.time() + 300
    except LunaUnavailable as error:
        phase, next_at = 'retry_wait', time.time() + error.retry_after
    except (CrawlError, ValueError, OSError, sqlite3.Error):
        phase, next_at = 'retry_wait', time.time() + 60
    finally:
        if started:
            state.release(started)
    return report(True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/data'))
    args = parser.parse_args()
    job = os.environ['POLICY_JOB_NAME']
    if not re.fullmatch(r'eqarchives-enrichment-' + identity() + r'-[1-9][0-9]{0,8}', job):
        raise ValueError('Invalid enrichment job identity')
    configured_workers = os.environ.get('LUNA_ENRICHMENT_WORKERS', str(WORKERS))
    if not re.fullmatch(r'[1-9][0-9]?', configured_workers) or int(configured_workers) > MAX_WORKERS:
        raise ValueError(f'Historical Luna workers must be between 1 and {MAX_WORKERS}')
    with closing(State('/indexing-state', identity())) as state, closing(MaintenanceServices()) as services, \
            Sources(['/archive-sources', '/archive'], '/data/publisher.git') as sources:
        result = run(args.root, state, services, sources, job, DailyBudget(os.environ['LUNA_DAILY_BUDGET_ROOT']),
                     workers=int(configured_workers))
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
