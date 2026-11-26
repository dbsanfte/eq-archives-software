"""Run the independent, bounded historical enrichment queue."""

import argparse
from collections import Counter, deque
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
from threading import Lock
import time

from common import CrawlError, Store, digest, save
from daily_budget import AutomaticStopped, DailyBudget, DailyBudgetPause, EnrichmentPriorityPause, LunaRatePause, install_scope_totals
from grading import Luna, LunaUnavailable
from import_status import EnrichmentError
from indexer import gemma, passages, search_enrichment
from indexer.capture_enrichment import Enricher, domain_prompts, summary_requests
from indexer.sqlite_state import SQLiteState
from policy_enrichment import COLLECTIONS, COUNTERS, FRONTIER_PAGE_SIZE, capture_identity, metadata_patch, prepare_refresh
from policy_maintenance import UPDATE_BATCH_SIZE, MaintenanceServices, YieldWork, foreground, identity
from policy_pool import PreparationPool, WorkerLost
from policy_sources import ServiceError, SourceError, Sources


WORKERS = 8
MAX_WORKERS = 192
FRONTIER_REFILL_AT = 32
MAX_RECORDS = 1200
BASELINE_WORKERS = 32
MAX_SLICE_RECORDS = MAX_RECORDS * MAX_WORKERS // BASELINE_WORKERS
SLICE_SECONDS = 720
LUNA_SECONDS = 45
MIN_REQUEST_TIME_LEFT = 15
SUMMARY_BATCH_SIZE = 16
SUMMARY_BATCH_WAIT = 1
PREPARATION_WORKERS = 2
PREPARATION_POLL = .01
EXECUTION_COUNTERS = ('embedding_waves', 'examined', 'checkpoint_views', 'rebuilt_views', 'frontier_queries',
                      'preparation_workers', 'preparation_retries', 'scope_budget_retries', 'scope_budget_exhausted')


def pending_scope(identifier):
    try:
        return digest(capture_identity(identifier)[1])
    except (SourceError, TypeError, ValueError):
        return ''  # Leave invalid identifiers to the normal per-record failure path.


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
                    scope TEXT, PRIMARY KEY(key,id))''')
                if 'scope' not in {row[1] for row in db.execute('PRAGMA table_info(pending)')}:
                    db.execute('ALTER TABLE pending ADD COLUMN scope TEXT')
                # Older images can still populate this queue after a rollback.
                missing_scopes = db.execute('SELECT id FROM pending WHERE key=? AND scope IS NULL', (self.key,)).fetchall()
                if missing_scopes:
                    db.executemany('UPDATE pending SET scope=? WHERE key=? AND id=?',
                                   [(pending_scope(identifier), self.key, identifier) for identifier, in missing_scopes])
                db.execute('CREATE INDEX IF NOT EXISTS pending_collection ON pending(key,collection,id)')
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

    def fill(self, services, check, *, refill_at=FRONTIER_REFILL_AT):
        now = time.time()
        db = self.db.connection()
        queries = 0
        for collection, cursor, next_at in db.execute('SELECT collection,cursor,next_at FROM frontiers WHERE key=?', (self.key,)).fetchall():
            if next_at > now:
                continue
            queued, = db.execute('SELECT COUNT(*) FROM pending WHERE key=? AND collection=?', (self.key, collection)).fetchone()
            if queued > refill_at:
                continue
            check()
            limit = FRONTIER_PAGE_SIZE - queued
            identifiers = services.select_enrichment(cursor, collection=collection, limit=limit)
            if len(identifiers) > limit or any(not identifier.startswith(collection + '/') for identifier in identifiers):
                raise ServiceError('response_identity')
            queries += 1
            items = [(self.key, collection, identifier, pending_scope(identifier)) for identifier in identifiers]
            with self.db.transaction(attempts=6) as db:
                # The cursor can only pass IDs once they are in the durable queue.
                db.executemany('INSERT OR IGNORE INTO pending(key,collection,id,scope) VALUES (?,?,?,?)', items)
                db.execute('UPDATE frontiers SET cursor=?,next_at=? WHERE key=? AND collection=?',
                           (identifiers[-1] if identifiers else '', 0 if identifiers else now + 900, self.key, collection))
        return queries

    def ready(self, rotation, limit, active=frozenset(), blocked_scopes=()):
        if limit <= 0:
            return []
        now = time.time()
        blocked = tuple(active)
        excluding = f"AND id NOT IN ({','.join('?' for _ in blocked)})" if blocked else ''
        if blocked_scopes:
            excluding += f" AND (scope IS NULL OR scope NOT IN ({','.join('?' for _ in blocked_scopes)}))"
        query = f'SELECT id FROM pending WHERE key=? AND collection=? AND retry_at<=? {excluding} ORDER BY id LIMIT ?'
        db = self.db.connection()
        rows = {collection: deque(row[0] for row in db.execute(
            query, (self.key, collection, now, *blocked, *blocked_scopes, limit)))
                for collection in COLLECTIONS}
        result = []
        while len(result) < limit and any(rows.values()):
            collection = next(rotation)
            if rows[collection]:
                result.append(rows[collection].popleft())
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
    for offset in range(0, len(records), UPDATE_BATCH_SIZE):
        group = records[offset:offset + UPDATE_BATCH_SIZE]
        updates = []
        for prepared, patch in group:
            identifier = prepared.hit['_id']
            update = checkpoint if patch is None else {**patch, **checkpoint, 'llm_summary_vector': summaries[identifier]}
            updates.append((prepared.hit, update))
        check()
        failed = None
        for (prepared, patch), error in zip(group, services.update_many(updates), strict=True):
            identifier = prepared.hit['_id']
            if error is None:
                yield identifier, 'reused' if patch is None else 'updated'
            elif isinstance(error, SourceError):
                yield identifier, error.reason
            else:
                failed = failed or error
        if failed:
            raise failed


def run(root, state, services, sources, job, account, *, seconds=SLICE_SECONDS, maximum=MAX_RECORDS, workers=WORKERS,
        enricher_factory=Enricher, should_yield=None, preparation_pool=None):
    if not 0 < seconds <= SLICE_SECONDS or not 1 <= maximum <= MAX_SLICE_RECORDS or not 1 <= workers <= MAX_WORKERS:
        raise ValueError('Invalid historical enrichment slice bounds')
    deadline = time.monotonic() + seconds
    should_yield = should_yield or (lambda: foreground(root, enrichment=True))
    phase, next_at = 'bounded', 0
    counters = {'luna_workers': workers, **dict.fromkeys(EXECUTION_COUNTERS, 0)}
    if preparation_pool:
        counters['preparation_workers'] = preparation_pool.workers
    rotation, capped = cycle(COLLECTIONS), set()
    scope_running, scope_limits, draining = Counter(), {}, set()
    started, initialized = set(), set()
    futures, scheduled = {}, {}
    preparing = {}
    outcomes = []
    buffered_at = 0
    # All graders update the same SQLite budget. Keep network I/O outside this lock.
    writer = Lock()

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
            return Luna(path, deadline=time.time() + LUNA_SECONDS, tls_context=tls_context)
        with closing(enricher_factory(cache, '/run/secrets/luna_api_key', client_factory=client,
                                     check=check_request, prepared_cache=True, writer=writer)) as enricher:
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

    def scope_blocked(scope):
        running = scope_running[scope]
        return running > 0 and (scope in draining or running >= scope_limits.get(scope, workers))

    def requeue(identifiers):
        # Capacity holds never consume one of the record's interruption attempts.
        state.release(identifiers)
        started.difference_update(identifiers)

    def submit_grade(pool, identifier, scope, prepared):
        if prepared is None or scope in capped:
            finish(identifier, 'skipped')
            return False
        counters['checkpoint_views' if prepared.checkpoint_view else 'rebuilt_views'] += 1
        if prepared.capture is None:
            completed(prepared, None)
            return False
        key = scope, prepared.capture['url'], prepared.capture['sha256'], digest(prepared.grounding)
        if key in scheduled:
            futures[scheduled[key]][1].append(prepared)
            return True
        if scope_blocked(scope):
            requeue([identifier])
            return False
        cache = state.root / 'policy-enrichment' / scope
        if scope not in initialized:
            with closing(Store(cache)) as store:
                install_scope_totals(store.db)
            initialized.add(scope)
        future = pool.submit(copy_context().run, grade, prepared, cache)
        scheduled[key] = future
        futures[future] = (key, [prepared], scope_limits.get(scope, workers))
        scope_running[scope] += 1
        return True

    def preparation_lost(identifier):
        counters['preparation_retries'] += 1
        # Count a crashed worker against this ID only; the next slice retains it.
        state.defer(identifier)
        started.discard(identifier)

    def admit(pool, limit):
        counters['frontier_queries'] += state.fill(services, check, refill_at=max(FRONTIER_REFILL_AT, workers))
        blocked_scopes = tuple(scope for scope in scope_limits if scope_blocked(scope))
        identifiers = state.ready(rotation, limit, started, blocked_scopes)
        for identifier in identifiers:
            check()
            counters['examined'] += 1
            if not state.begin(identifier):
                finish(identifier, 'failed', 'repeated_worker_interruption')
                continue
            started.add(identifier)
            try:
                url, scope = capture_identity(identifier)
                scope = digest(scope)
                if preparation_pool and scope not in capped:
                    hit = services.get(identifier)
                    preparing[identifier] = (preparation_pool.submit((hit, url)), scope)
                    continue
                prepared = None if scope in capped else prepare_refresh(services, sources, identifier, url, check)
            except SourceError as error:
                finish(identifier, error.reason)
                continue
            except ServiceError:
                raise
            except WorkerLost:
                preparation_lost(identifier)
                continue
            except (TypeError, KeyError, ValueError):
                finish(identifier, 'failed', 'source_validation')
                continue
            submit_grade(pool, identifier, scope, prepared)
        return bool(identifiers)

    def collect_preparations(pool, capacity):
        for identifier, (task, scope) in tuple(preparing.items()):
            if capacity <= 0:
                break
            if task.ready():
                del preparing[identifier]
                try:
                    prepared = task.result(check)
                except SourceError as error:
                    if error.reason == 'preparation_timeout':
                        preparation_lost(identifier)
                    else:
                        finish(identifier, error.reason)
                except WorkerLost:
                    preparation_lost(identifier)
                else:
                    if prepared is not None and prepared.hit['_id'] != identifier:
                        raise ServiceError('preparation_identity')
                    capacity -= submit_grade(pool, identifier, scope, prepared)
        return capacity

    def collect(pause):
        for future in tuple(futures):
            if not future.done():
                continue
            key, records, submitted_limit = futures.pop(future)
            del scheduled[key]
            scope = key[0]
            running = scope_running[scope]
            scope_running[scope] -= 1
            if running == 1:
                del scope_running[scope]
                draining.discard(scope)
            try:
                patch = future.result()
            except EnrichmentError as error:
                if error.code == 'budget':
                    if submitted_limit > 1:
                        # The other reservations can still settle and free room.
                        # Drain this scope, then reduce overlap and recheck the exact records.
                        if submitted_limit == scope_limits.get(scope, workers):
                            scope_limits[scope] = max(1, running // 2)
                        if running > 1:
                            draining.add(scope)
                        requeue([prepared.hit['_id'] for prepared in records])
                        counters['scope_budget_retries'] += len(records)
                        continue
                    # A grade launched at a width of one had no peer reservations
                    # from this job. Its denial still counts historical uncertainty.
                    capped.add(scope)
                    counters['scope_budget_exhausted'] += 1
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

    def process(pool):
        nonlocal phase, next_at
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
            capacity = 0
            if not pause:
                check()
                capacity = workers - sum(len(records) for _, records, _ in futures.values())
                capacity = collect_preparations(pool, capacity)
                # Each process can hold one prepared result while grading is full.
                available = preparation_pool.workers - len(preparing) if preparation_pool else capacity
                limit = min(available, maximum - counters['examined'])
                if limit > 0 and admit(pool, limit):
                    continue
            if not futures and (pause or not preparing):
                if pause:
                    raise pause
                if counters['examined'] < maximum:
                    phase, next_at = 'sweep_waiting', max(time.time() + 10, state.next_at())
                break
            wait_time = max(0, buffered_at + SUMMARY_BATCH_WAIT - now) if outcomes else SUMMARY_BATCH_WAIT
            if preparing and capacity > 0:
                wait_time = min(wait_time, PREPARATION_POLL)
            if futures:
                wait(futures, timeout=wait_time, return_when=FIRST_COMPLETED)
            elif wait_time:
                time.sleep(wait_time)

    try:
        budget = account.snapshot(maintenance=True)
        if not budget['complete']:
            phase, next_at = 'waiting_configuration', time.time() + 300
            return report(True)
        if budget['remaining_usd'] <= 0:
            phase, next_at = 'waiting_budget', datetime.fromisoformat(budget['resets_at']).timestamp()
            return report(True)
        if budget['luna_retry_at']:
            phase, next_at = 'retry_wait', datetime.fromisoformat(budget['luna_retry_at']).timestamp()
            return report(True)
        if budget['enrichment_pending']:
            phase, next_at = 'waiting_imports', time.time() + 60
            return report(True)
        tls_context = Luna.tls_context()
        domain_prompts('')  # Load bundled templates before the grading threads start.
        # The pool drains before its shared priority lease is released, even
        # when the slice yields or encounters a failed write.
        with account.bind(purpose='maintenance', owner=job), ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                process(pool)
            finally:
                if preparation_pool:
                    preparation_pool.cancel_pending()
    except YieldWork:
        phase = 'yielded'
    except LunaRatePause as error:
        phase, next_at = 'retry_wait', datetime.fromisoformat(error.retry_at).timestamp()
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
    if not re.fullmatch(r'[1-9][0-9]{0,2}', configured_workers) or int(configured_workers) > MAX_WORKERS:
        raise ValueError(f'Historical Luna workers must be between 1 and {MAX_WORKERS}')
    workers = int(configured_workers)
    maximum = max(MAX_RECORDS, MAX_RECORDS * workers // BASELINE_WORKERS)
    with closing(State('/indexing-state', identity())) as state, closing(MaintenanceServices()) as services, \
            Sources(['/archive-sources', '/archive'], '/data/publisher.git') as sources, \
            closing(PreparationPool(sources.roots, sources.publication, PREPARATION_WORKERS,
                                    enrichment=True, tasks=256, timeout=30)) as preparation_pool:
        result = run(args.root, state, services, sources, job, DailyBudget(os.environ['LUNA_DAILY_BUDGET_ROOT']),
                     workers=workers, maximum=maximum, preparation_pool=preparation_pool)
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
