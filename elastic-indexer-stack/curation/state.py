"""Durable review state and an exclusive worker lease, outside archive Git."""

from contextlib import contextmanager
from enum import Enum
import fcntl
import json
from pathlib import Path
import re
import uuid

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
            CREATE INDEX IF NOT EXISTS operation_activity ON operations(updated DESC);
            CREATE INDEX IF NOT EXISTS batch_activity ON batches(updated DESC);
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
