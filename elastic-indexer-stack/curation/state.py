"""Durable review state and an exclusive worker lease, outside archive Git."""

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import uuid

from common import CrawlError, Store, now


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
    if commit:
        store.db.execute("BEGIN IMMEDIATE")
    if kind != 'publish' and store.db.execute("SELECT 1 FROM operations WHERE state IN ('queued','running')").fetchone():
        raise CrawlError("A capture or discovery operation is already queued or running")
    operation = identifier()
    store.db.execute("INSERT INTO operations VALUES (?,?,?, ?,NULL,NULL,?,?)",
                     (operation, kind, "queued", json.dumps(payload), now(), now()))
    if commit:
        store.db.commit()
    return operation
