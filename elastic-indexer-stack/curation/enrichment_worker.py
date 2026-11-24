"""Run the independent, bounded historical enrichment queue."""

import argparse
from concurrent.futures import ThreadPoolExecutor, wait
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

from common import CrawlError, digest, save
from daily_budget import AutomaticStopped, DailyBudget, DailyBudgetPause, EnrichmentPriorityPause
from grading import Luna
from import_status import EnrichmentError
from indexer import gemma, passages, search_enrichment
from indexer.capture_enrichment import Enricher, summary_requests
from indexer.sqlite_state import SQLiteState
from policy_enrichment import COLLECTIONS, COUNTERS, capture_identity, metadata_patch, prepare_refresh
from policy_maintenance import MaintenanceServices, YieldWork, foreground, identity
from policy_sources import ServiceError, SourceError, Sources


WORKERS = 4
PAGE_SIZE = 32
MAX_RECORDS = 512
SLICE_SECONDS = 480


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
            if next_at > now or db.execute('SELECT 1 FROM pending WHERE key=? AND collection=? LIMIT 1', (self.key, collection)).fetchone():
                continue
            check()
            identifiers = services.select_enrichment(cursor, collection=collection, limit=PAGE_SIZE)
            if any(not identifier.startswith(collection + '/') for identifier in identifiers):
                raise ServiceError('response_identity')
            with self.db.transaction(attempts=6) as db:
                # The cursor can only pass IDs once they are in the durable queue.
                db.executemany('INSERT INTO pending(key,collection,id) VALUES (?,?,?)',
                               [(self.key, collection, identifier) for identifier in identifiers])
                db.execute('UPDATE frontiers SET cursor=?,next_at=? WHERE key=? AND collection=?',
                           (identifiers[-1] if identifiers else '', 0 if identifiers else now + 900, self.key, collection))

    def ready(self, rotation, limit):
        rows = {name: [] for name in COLLECTIONS}
        for collection, identifier in self.db.connection().execute(
                'SELECT collection,id FROM pending WHERE key=? AND retry_at<=? ORDER BY id', (self.key, time.time())):
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
    """Embed one wave and validate the whole response before the first write."""
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


def run(root, state, services, sources, job, account, *, seconds=SLICE_SECONDS, maximum=MAX_RECORDS,
        enricher_factory=Enricher, should_yield=None):
    if not 0 < seconds <= SLICE_SECONDS or not 1 <= maximum <= MAX_RECORDS:
        raise ValueError('Invalid historical enrichment slice bounds')
    deadline = time.monotonic() + seconds
    should_yield = should_yield or (lambda: foreground(root, enrichment=True))
    phase, next_at = 'bounded', 0
    counters = {'luna_workers': WORKERS, 'embedding_waves': 0, 'examined': 0}
    rotation, capped = cycle(COLLECTIONS), set()
    started = []

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

    def grade(prepared, scope):
        def client(path):
            return Luna(path, deadline=time.time() + max(0, min(45, deadline - time.monotonic())))
        with closing(enricher_factory(state.root / 'policy-enrichment' / digest(scope), '/run/secrets/luna_api_key',
                                     client_factory=client, check=check)) as enricher:
            return metadata_patch(enricher.enrich_for_maintenance(prepared.capture, prepared.source, prepared.grounding))

    def request_key(prepared, scope):
        return scope, prepared.capture['url'], prepared.capture['sha256'], digest(prepared.grounding)

    def wave(identifiers, pool):
        outcomes, paid = [], []
        for identifier in identifiers:
            check()
            if not state.begin(identifier):
                state.finish(identifier, 'failed', 'repeated_worker_interruption')
                continue
            started.append(identifier)
            try:
                url, scope = capture_identity(identifier)
                prepared = None if scope in capped else prepare_refresh(services, sources, identifier, url, check)
                if prepared is None:
                    state.finish(identifier, 'skipped')
                elif prepared.capture is None:
                    outcomes.append((prepared, None))
                else:
                    paid.append((prepared, scope))
            except SourceError as error:
                if error.reason == 'concurrent_update':
                    state.defer(identifier)
                else:
                    state.finish(identifier, 'failed', error.reason)
            except ServiceError:
                raise
            except (TypeError, KeyError, ValueError):
                state.finish(identifier, 'failed', 'source_validation')

        pause = None
        # All sibling contexts share one priority owner. Release the claim only
        # after every response has been drained and its cost accounted for.
        if paid:
            with account.bind(purpose='maintenance', owner=job):
                futures = {}
                try:
                    for prepared, scope in paid:
                        key = request_key(prepared, scope)
                        if key not in futures:
                            futures[key] = pool.submit(copy_context().run, grade, prepared, scope)
                    for prepared, scope in paid:
                        identifier = prepared.hit['_id']
                        try:
                            outcomes.append((prepared, futures[request_key(prepared, scope)].result()))
                        except EnrichmentError as error:
                            if error.code == 'budget':
                                capped.add(scope)
                                state.finish(identifier, 'skipped', 'enrichment_budget')
                            else:
                                state.finish(identifier, 'failed', 'enrichment_' + error.code)
                        except Exception as error:
                            pause = pause or error
                finally:
                    wait(futures.values())
        if outcomes:
            counters['embedding_waves'] += int(any(patch is not None for _, patch in outcomes))
            for identifier, result in write_wave(services, outcomes, check):
                if result == 'concurrent_update':
                    state.defer(identifier)
                else:
                    state.finish(identifier, result if result in COUNTERS else 'failed',
                                 None if result in COUNTERS else result)
        if pause:
            raise pause

    try:
        budget = account.snapshot()
        if not budget['complete']:
            phase, next_at = 'waiting_configuration', time.time() + 300
            return report(True)
        if budget['remaining_usd'] <= 0:
            phase, next_at = 'waiting_budget', datetime.fromisoformat(budget['resets_at']).timestamp()
            return report(True)
        if budget['enrichment_pending']:
            phase, next_at = 'waiting_imports', time.time() + 60
            return report(True)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            last_report = 0
            while counters['examined'] < maximum:
                check()
                state.fill(services, check)
                identifiers = state.ready(rotation, min(WORKERS, maximum - counters['examined']))
                if not identifiers:
                    phase, next_at = 'sweep_waiting', max(time.time() + 10, state.next_at())
                    break
                wave(identifiers, pool)
                started.clear()
                counters['examined'] += len(identifiers)
                if time.monotonic() - last_report >= 5:
                    report()
                    last_report = time.monotonic()
    except YieldWork:
        phase = 'yielded'
    except DailyBudgetPause as error:
        phase, next_at = 'waiting_budget', datetime.fromisoformat(error.retry_at).timestamp()
    except EnrichmentPriorityPause:
        phase, next_at = 'waiting_imports', time.time() + 60
    except AutomaticStopped:
        phase, next_at = 'waiting_configuration', time.time() + 300
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
    with closing(State('/indexing-state', identity())) as state, closing(MaintenanceServices()) as services, \
            Sources(['/archive-sources', '/archive'], '/data/publisher.git') as sources:
        result = run(args.root, state, services, sources, job, DailyBudget(os.environ['LUNA_DAILY_BUDGET_ROOT']))
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
