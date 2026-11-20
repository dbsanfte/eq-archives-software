"""Capture inventory counts and a current-attempt throughput estimate.

Only saved metadata is used. Estimates exclude reused sources and paused time;
the worker persists each result so every browser sees the same progress.
"""
from collections import deque
from math import ceil
from time import monotonic

from common import now


def initialize_inventory_counts(db):
    """Materialize state counts once and maintain them with the file transaction."""
    if db.execute("SELECT 1 FROM meta WHERE key='full_record_counts_v1'").fetchone():
        return
    db.executescript('''
        BEGIN;
        CREATE TABLE IF NOT EXISTS full_record_counts (
            state TEXT PRIMARY KEY, files INTEGER NOT NULL, bytes INTEGER NOT NULL);
        INSERT INTO full_record_counts(state,files,bytes)
            SELECT state,COUNT(*),SUM(CASE WHEN state='captured'
                THEN COALESCE(json_extract(capture,'$.bytes'),0) ELSE 0 END)
            FROM full_records
            GROUP BY state;
        INSERT OR IGNORE INTO meta VALUES ('full_record_counts_v1','true');
        CREATE TRIGGER IF NOT EXISTS full_record_count_insert AFTER INSERT ON full_records BEGIN
            INSERT INTO full_record_counts VALUES (new.state,1,COALESCE(
                CASE WHEN new.state='captured' THEN json_extract(new.capture,'$.bytes') END,0))
            ON CONFLICT(state) DO UPDATE SET files=files+1,bytes=bytes+excluded.bytes;
        END;
        CREATE TRIGGER IF NOT EXISTS full_record_count_update AFTER UPDATE OF state,capture ON full_records BEGIN
            UPDATE full_record_counts SET files=files-1,bytes=bytes-COALESCE(
                CASE WHEN old.state='captured' THEN json_extract(old.capture,'$.bytes') END,0) WHERE state=old.state;
            INSERT INTO full_record_counts VALUES (new.state,1,COALESCE(
                CASE WHEN new.state='captured' THEN json_extract(new.capture,'$.bytes') END,0))
            ON CONFLICT(state) DO UPDATE SET files=files+1,bytes=bytes+excluded.bytes;
        END;
        CREATE TRIGGER IF NOT EXISTS full_record_count_delete AFTER DELETE ON full_records BEGIN
            UPDATE full_record_counts SET files=files-1,bytes=bytes-COALESCE(
                CASE WHEN old.state='captured' THEN json_extract(old.capture,'$.bytes') END,0) WHERE state=old.state;
        END;
        COMMIT;
    ''')


class CaptureProgress:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.points = deque(maxlen=30)
        self.series = None

    def update(self, snapshot):
        board = snapshot.get('ezboard') or snapshot.get('sitepowerup')
        total, remaining = snapshot.get('versions_found'), snapshot.get('versions_pending')
        if board:
            counts = board.get('counts', {})
            total = sum(counts.values())
            remaining = counts.get('pending', 0) + counts.get('downloaded', 0) + counts.get('retry', 0)
        known = (type(total) is int and type(remaining) is int and 0 <= remaining <= total)
        completed = total - remaining if known else None
        estimate = {'total': total if known else None, 'remaining': remaining if known else None,
                    'completed': completed, 'eta_seconds': None, 'updated_at': now()}
        series = snapshot.get('capture_policy') or ('board' if board else 'legacy')
        instant = self.clock()
        if series != self.series or not known or (self.points and completed < self.points[-1][1]):
            self.points.clear()
        self.series = series
        if known and (snapshot.get('phase') == 'downloading' or self.points):
            if not self.points or completed != self.points[-1][1]:
                self.points.append((instant, completed))
            elapsed = instant - self.points[0][0]
            processed = completed - self.points[0][1]
            if elapsed > 0 and processed > 0 and remaining:
                estimate['eta_seconds'] = ceil(remaining * elapsed / processed)
        return {**snapshot, 'completion': estimate}
