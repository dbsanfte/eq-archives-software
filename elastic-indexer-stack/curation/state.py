"""Durable review state and an exclusive worker lease, outside archive Git."""

from contextlib import contextmanager
from enum import Enum
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import uuid
import time
from functools import wraps

from common import CrawlError, Store, now


class Lane(str, Enum):
    CANDIDATES = 'candidates'
    CAPTURE = 'capture'
    INDEXING = 'indexing'


OPERATION_KINDS = {
    Lane.CANDIDATES: ('discover', 'candidate_check'),
    Lane.CAPTURE: ('capture',),
    Lane.INDEXING: ('publish',),
}


def database_busy(error):
    """Retry contention only; corruption, missing tables and I/O errors surface."""
    code=getattr(error,'sqlite_errorcode',None)
    return isinstance(error,sqlite3.OperationalError) and (
        code is not None and (code & 255) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED)
        or code is None and str(error) in ('database is locked','database table is locked','database schema is locked'))


def retry_metadata(action):
    """Retry a closed metadata transaction, never a download or paid request."""
    @wraps(action)
    def run(*args, **kwargs):
        for attempt in range(4):
            try:
                return action(*args, **kwargs)
            except sqlite3.OperationalError as error:
                if not database_busy(error) or attempt == 3:
                    raise
                time.sleep(0.05 * 2 ** attempt)
    return run


def operation_lane(kind):
    for lane, kinds in OPERATION_KINDS.items():
        if kind in kinds:
            return lane
    raise CrawlError('Unknown operation kind')


def active_operation(store, lane=Lane.CAPTURE, *, paused_capture=False, states=('queued', 'running')):
    """Read uncapped worker state; callers hold a transaction before claiming."""
    kinds = OPERATION_KINDS[Lane(lane)]
    return store.db.execute(f"""SELECT * FROM operations
        WHERE kind IN ({','.join('?' for _ in kinds)})
          AND (state IN ({','.join('?' for _ in states)})
               OR (? AND kind='capture' AND state='interrupted'))
        ORDER BY CASE state WHEN 'interrupted' THEN 0 WHEN 'running' THEN 1 ELSE 2 END,
                 CASE WHEN kind='publish' THEN CASE json_extract(result,'$.progress.phase')
                     WHEN 'pushing' THEN 0 WHEN 'merging_trees' THEN 1 WHEN 'checking_head' THEN 1
                     WHEN 'ready' THEN 2 ELSE 3 END END,
                 created,rowid LIMIT 1""", (*kinds, *states, paused_capture)).fetchone()


def identifier():
    return uuid.uuid4().hex


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise CrawlError("Invalid operation or batch ID")
    return value


@contextmanager
def connect(directory):
    store = Store(directory)
    try:
        store.db.executescript("""
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, kind TEXT, state TEXT,
                payload TEXT, result TEXT, error TEXT, created TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, state TEXT, manifest TEXT,
                manifest_sha256 TEXT, publication TEXT, job TEXT, error TEXT, created TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS capture_queue_failures(candidate TEXT PRIMARY KEY,
                decision TEXT NOT NULL, error TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS capture_retries(operation TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL, retry_at TEXT, paused INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS capture_legacy_recovery(operation TEXT PRIMARY KEY,
                recovered_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS publication_retries(operation TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL, retry_at TEXT, paused INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS publication_sizes(batch TEXT PRIMARY KEY,
                manifest_sha256 TEXT NOT NULL, files INTEGER NOT NULL, bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_storage_retries(operation TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL, retry_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS operation_publication_batch ON operations(kind,json_extract(payload,'$.batch_id'));
            CREATE INDEX IF NOT EXISTS operation_activity ON operations(updated DESC);
            CREATE INDEX IF NOT EXISTS batch_activity ON batches(updated DESC);
            CREATE INDEX IF NOT EXISTS batch_work_state ON batches(state,created,id);
            CREATE INDEX IF NOT EXISTS batch_metadata ON batches(
                id,state,manifest_sha256,publication,job,error,created,updated);
        """)
        yield store
    finally:
        store.close()


def unpack(row):
    result = dict(row)
    for key in ("payload", "result", "manifest", "publication", "job"):
        if key in result:
            result[key] = json.loads(result[key]) if result[key] else None
    return result


def worker_lease(directory):
    path = Path(directory) / "worker.lock"
    handle = path.open("a")
    path.chmod(0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise CrawlError("Another worker already owns this staging directory") from None
    return handle


def enqueue(store, kind, payload, commit=True):
    lane = operation_lane(kind)
    if commit:
        store.db.execute("BEGIN IMMEDIATE")
    if lane != Lane.INDEXING and active_operation(store, lane):
        raise CrawlError('Candidate discovery or evidence grading is already queued or running; wait for it to finish'
                         if lane == Lane.CANDIDATES else 'Capture is already queued or running; wait for it to finish')
    operation = identifier()
    store.db.execute("INSERT INTO operations VALUES (?,?,?, ?,NULL,NULL,?,?)",
                     (operation, kind, "queued", json.dumps(payload), now(), now()))
    if commit:
        store.db.commit()
    return operation
