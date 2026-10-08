"""Capture inventory counts and a current-attempt throughput estimate.

Only saved metadata is used. Estimates exclude reused sources and paused time;
the worker persists each result so every browser sees the same progress.
"""
from collections import deque
from math import ceil
from time import monotonic

from common import now


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
            remaining = counts.get('pending', 0) + counts.get('downloaded', 0)
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
