"""Cached, read-only totals from the portal's paid-request ledgers, never sources."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time

from daily_budget import SCALE


class Spending:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.cache = {}
        self.result = None
        self.expires = 0

    def ledgers(self):
        yield self.root / 'crawl.sqlite3'
        # Only two shallow metadata directories. Never enumerate captured pages
        # or an archive checkout. An uninitialized campaign still contains a
        # backup of the pilot ledger and must not be counted twice.
        for folder in ('runs', 'enrichment'):
            parent = self.root / folder
            if not parent.exists():
                continue
            for child in parent.iterdir():
                if (re.fullmatch(r'[a-f0-9]{32}', child.name) and not child.is_symlink()
                        and (folder != 'runs' or (child / 'initialized').is_file())):
                    path = child / 'crawl.sqlite3'
                    if path.exists():
                        yield path

    @staticmethod
    def fingerprint(path):
        stamps = []
        for item in (path, Path(str(path) + '-wal')):
            try:
                stat = item.stat()
                stamps.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                stamps.append(None)
        return tuple(stamps)

    def snapshot(self, instant=None):
        instant = (instant or datetime.now(timezone.utc)).astimezone(timezone.utc)
        today = instant.replace(hour=0, minute=0, second=0, microsecond=0)
        month = today.replace(day=1)
        period = (today.isoformat(), month.isoformat())
        with self.lock:
            if self.result and self.result['day'] == period[0] and time.monotonic() < self.expires:
                return self.result
            deadline = time.monotonic() + 2
            totals = {key: {'estimated_usd': 0, 'unresolved_usd': 0} for key in ('today', 'month')}
            complete = True
            seen = set()
            try:
                shared = self.shared(today, month, deadline)
                if shared is not None:
                    totals = shared
                for path in (() if shared is not None else self.ledgers()):
                    if time.monotonic() > deadline:
                        complete = False
                        break
                    stamp = (period, self.fingerprint(path))
                    seen.add(path)
                    cached = self.cache.get(path)
                    if cached and cached[0] == stamp:
                        values = cached[1]
                    else:
                        try:
                            values = self.read(path, today, month, instant, deadline)
                        except (OSError, sqlite3.Error, ValueError):
                            complete = False
                            continue
                        self.cache[path] = (stamp, values)
                    for key in totals:
                        for field in totals[key]:
                            totals[key][field] += values[key][field]
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                complete = False
            self.cache = {path: value for path, value in self.cache.items() if path in seen}
            self.result = {**totals, 'complete': complete, 'currency': 'USD', 'timezone': 'UTC',
                           'day': period[0], 'month_start': period[1], 'as_of': instant.isoformat()}
            self.expires = time.monotonic() + (10 if complete else 1)
            return self.result

    def shared(self, today, month, deadline):
        """Use the same seeded, atomic ledger as admission once configured.

        This also counts reservations whose local attempt transaction was lost.
        Before configuration, legacy per-operation ledgers remain authoritative.
        Opening read-only never initializes accounting or changes settings.
        """
        path = self.root / 'luna-budget' / 'budget.sqlite3'
        if not path.exists():
            return None
        database = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)
        try:
            database.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            database.execute('BEGIN')
            row = database.execute('SELECT value FROM settings WHERE id=1').fetchone()
            if not row:
                raise ValueError('Shared accounting settings are unavailable')
            if not json.loads(row[0]).get('configured'):
                return None
            # Range uses reservation_day. All four totals share one read snapshot,
            # with integer nanodollars and the admission ledger's UTC boundaries.
            row = database.execute('''SELECT
                COALESCE(SUM(CASE WHEN day=? THEN COALESCE(actual,0) ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN day=? AND actual IS NULL THEN reserved ELSE 0 END),0),
                COALESCE(SUM(actual),0),
                COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN typeof(reserved)!='integer' OR reserved<0 OR
                    (actual IS NOT NULL AND (typeof(actual)!='integer' OR actual<0)) THEN 1 ELSE 0 END),0)
                FROM reservations WHERE day>=? AND day<=?''',
                (today.date().isoformat(), today.date().isoformat(),
                 month.date().isoformat(), today.date().isoformat())).fetchone()
            if row[4]:
                raise ValueError('Invalid shared cost record')
            return {'today': {'estimated_usd': row[0]/SCALE, 'unresolved_usd': row[1]/SCALE},
                    'month': {'estimated_usd': row[2]/SCALE, 'unresolved_usd': row[3]/SCALE}}
        finally:
            database.close()

    @staticmethod
    def read(path, today, month, instant, deadline):
        result = {key: {'estimated_usd': 0, 'unresolved_usd': 0} for key in ('today', 'month')}
        database = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)
        try:
            database.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            # Include every attempt, even rejected responses and corrections.
            # Cached/retried results have no new attempt and cannot add spend.
            rows = database.execute('SELECT actual,reserved,created FROM attempts WHERE datetime(created)>=datetime(?)',
                                    (month.isoformat(),))
            for actual, reserved, created in rows:
                when = datetime.fromisoformat(created).astimezone(timezone.utc)
                if when > instant:
                    continue
                amount = reserved if actual is None else actual
                if not isinstance(amount, (float, int)) or not math.isfinite(amount) or amount < 0:
                    raise ValueError('Invalid cost record')
                field = 'unresolved_usd' if actual is None else 'estimated_usd'
                result['month'][field] += amount
                if when >= today:
                    result['today'][field] += amount
            return result
        finally:
            database.close()
