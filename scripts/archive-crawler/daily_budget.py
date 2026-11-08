"""Durable UTC daily/monthly Luna ceilings and enrichment-first admission.

Reserve before sending, retain uncertain requests, and release only verified
usage. A lost local commit can leak a conservative reservation, never a payment
outside the daily ceiling. The database contains metadata only, not sources/keys.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import json
import math
from pathlib import Path
import sqlite3
import uuid

from common import CrawlError, now

SCALE = 1_000_000_000
DEFAULT = {'enabled': False, 'configured': False, 'daily_usd': 2.0, 'monthly_usd': 200.0,
           'min_grade': 2, 'grading_criteria': '', 'revision': 0}
current_budget = ContextVar('luna_daily_budget', default=None)
automatic_request = ContextVar('automatic_luna_request', default=False)
request_purpose = ContextVar('luna_request_purpose', default='discovery')
priority_owner = ContextVar('luna_priority_owner', default=None)


def instant():
    return datetime.now(timezone.utc)


def units(amount):
    if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
        raise CrawlError('Invalid Luna spend accounting; paid work is paused')
    return int((Decimal(str(amount)) * SCALE).to_integral_value(rounding=ROUND_CEILING))


class DailyBudgetPause(CrawlError):
    def __init__(self, amount, retry_at, period='daily'):
        self.amount, self.retry_at, self.period = amount, retry_at, period
        super().__init__(f'{period.title()} Luna budget reached; paid work resumes after the UTC {period} reset')


class EnrichmentPriorityPause(CrawlError):
    def __init__(self):
        super().__init__('Approved-site enrichment has priority; grading resumes automatically afterward')


class AutomaticStopped(CrawlError):
    def __init__(self):
        super().__init__('Automatic mode is paused; saved progress is retained')


class DailyBudget:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        with self.database() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, day TEXT NOT NULL,
                    reserved INTEGER NOT NULL, actual INTEGER, created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS reservation_day ON reservations(day);
                CREATE TABLE IF NOT EXISTS enrichment_priority(owner TEXT PRIMARY KEY, expires TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (json.dumps(DEFAULT),))
        (self.root / 'budget.sqlite3').chmod(0o600)

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.root / 'budget.sqlite3', timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def bind(self, *, automatic=False, purpose='discovery', owner=None):
        if purpose not in ('discovery','enrichment'):
            raise ValueError('Invalid Luna request purpose')
        token = current_budget.set(self)
        auto = automatic_request.set(automatic)
        scope = request_purpose.set(purpose)
        owner = owner or 'import-' + uuid.uuid4().hex
        claim=priority_owner.set(owner if purpose=='enrichment' else None)
        try:
            if purpose == 'enrichment':self.prioritize(owner, True)
            yield self
        finally:
            try:
                if purpose == 'enrichment':self.prioritize(owner, False)
            finally:
                priority_owner.reset(claim);request_purpose.reset(scope)
                automatic_request.reset(auto);current_budget.reset(token)

    def prioritize(self, owner, pending):
        if not isinstance(owner,str) or not 1<=len(owner)<=253 or type(pending) is not bool:
            raise ValueError('Invalid enrichment priority claim')
        with self.database() as db:
            if pending:
                when=instant();expires=(when+timedelta(minutes=5)).isoformat()
                db.execute('''INSERT INTO enrichment_priority VALUES (?,?) ON CONFLICT(owner) DO UPDATE
                    SET expires=excluded.expires WHERE enrichment_priority.expires<?''',
                    (owner,expires,(when+timedelta(minutes=4)).isoformat()))
            else:db.execute('DELETE FROM enrichment_priority WHERE owner=?',(owner,))

    def settings(self):
        with self.database() as db:
            return {**DEFAULT, **json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])}

    @staticmethod
    def validate(payload):
        from grading import criteria
        if (not isinstance(payload, dict) or set(payload)-{'monthly_usd'} != {'enabled', 'daily_usd', 'min_grade', 'grading_criteria', 'revision'}
                or type(payload['enabled']) is not bool or type(payload['min_grade']) is not int
                or payload['min_grade'] not in range(4) or type(payload['revision']) is not int
                or not 0 < units(payload['daily_usd']) <= 1_000_000 * SCALE
                or not 0 < units(payload.get('monthly_usd',DEFAULT['monthly_usd'])) <= 1_000_000 * SCALE):
            raise CrawlError('Automatic mode needs a daily USD limit, a minimum grade from 0 to 3, and current settings')
        return {**payload, 'grading_criteria': criteria(payload['grading_criteria'])}

    def configure(self, payload):
        payload = self.validate(payload)
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
            if payload['revision'] != previous['revision']:
                raise CrawlError('Automatic settings changed in another session. Reload the settings before saving.')
            saved = {**DEFAULT, **previous, **payload, 'configured': True, 'revision': previous['revision'] + 1, 'updated_at': now()}
            db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(saved),))
        return saved

    @staticmethod
    def totals(db, day):
        row = db.execute('''SELECT COALESCE(SUM(actual),0),
            COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0)
            FROM reservations WHERE day=?''', (day,)).fetchone()
        return tuple(row)

    @staticmethod
    def limits(db, when, config):
        day=when.date().isoformat();first=when.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        month_reset=(first+timedelta(days=32)).replace(day=1)
        month=db.execute('''SELECT COALESCE(SUM(actual),0),
            COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0)
            FROM reservations WHERE day>=? AND day<?''',
            (first.date().isoformat(),month_reset.date().isoformat())).fetchone()
        spent,pending=DailyBudget.totals(db,day)
        daily=units(config['daily_usd'])-spent-pending
        monthly=units(config.get('monthly_usd',DEFAULT['monthly_usd']))-sum(month)
        day_reset=(when+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        period='monthly' if monthly<daily else 'daily'
        return {'estimated_usd':spent/SCALE,'unresolved_usd':pending/SCALE,
                '_remaining_units':max(0,min(daily,monthly)),
                'remaining_usd':max(0,min(daily,monthly))/SCALE,'budget_period':period,
                'resets_at':(month_reset if period=='monthly' else day_reset).isoformat(),
                'daily_remaining_usd':max(0,daily)/SCALE,'daily_resets_at':day_reset.isoformat(),
                'monthly':{'estimated_usd':month[0]/SCALE,'unresolved_usd':month[1]/SCALE,
                           'remaining_usd':max(0,monthly)/SCALE,'resets_at':month_reset.isoformat()}}

    def snapshot(self):
        when = instant()
        with self.database() as db:
            config = {**DEFAULT,**json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])}
            limits=self.limits(db,when,config)
            priority=db.execute('SELECT 1 FROM enrichment_priority WHERE expires>? LIMIT 1',(when.isoformat(),)).fetchone() is not None
        return {'settings': config, 'complete': config['configured'], **{k:v for k,v in limits.items() if not k.startswith('_')},
                'enrichment_pending':priority,'day': when.date().isoformat(), 'timezone': 'UTC'}

    def reserve(self, amount):
        reserved = units(amount)
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            when = instant()
            day = when.date().isoformat()
            config = json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
            if automatic_request.get() and not config['enabled']:
                raise AutomaticStopped()
            limits=self.limits(db,when,config)
            if config['configured'] and reserved>limits['_remaining_units']:
                raise DailyBudgetPause(amount, limits['resets_at'], limits['budget_period'])
            if request_purpose.get()=='discovery' and db.execute(
                    'SELECT 1 FROM enrichment_priority WHERE expires>? LIMIT 1',(when.isoformat(),)).fetchone():
                raise EnrichmentPriorityPause()
            if owner:=priority_owner.get():
                db.execute('INSERT OR REPLACE INTO enrichment_priority VALUES (?,?)',
                           (owner,(when+timedelta(minutes=5)).isoformat()))
            receipt = uuid.uuid4().hex
            db.execute('INSERT INTO reservations VALUES (?,?,?,NULL,?)', (receipt, day, reserved, when.isoformat()))
        return receipt

    def settle(self, receipt, actual):
        actual = units(actual)
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT actual FROM reservations WHERE id=?', (receipt,)).fetchone()
            if previous is None or previous[0] is not None and previous[0] != actual:
                raise CrawlError('Luna reservation accounting changed; paid work is paused')
            db.execute('UPDATE reservations SET actual=? WHERE id=?', (actual, receipt))

    def import_legacy(self, identifier, reserved, actual, created, receipt=None):
        """Idempotently count pre-deployment paid requests when first enabled."""
        when = datetime.fromisoformat(created).astimezone(timezone.utc)
        reserved = units(reserved)
        actual = units(actual) if actual is not None else None
        with self.database() as db:
            if receipt:
                if not db.execute('SELECT 1 FROM reservations WHERE id=?', (receipt,)).fetchone():
                    raise CrawlError('A saved Luna reservation is unavailable; daily accounting is incomplete')
                return
            db.execute('''INSERT INTO reservations VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE
                SET actual=CASE WHEN reservations.actual IS NULL THEN excluded.actual ELSE reservations.actual END''',
                ('legacy:' + identifier, when.date().isoformat(), reserved, actual, created))


def reserve_attempt(store, attempt, amount):
    """Call while holding the operation's write transaction, before commit/send."""
    if budget := current_budget.get():
        try:
            receipt = budget.reserve(amount)
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                             ('daily_reservation:' + str(attempt), json.dumps(receipt)))
        except BaseException:
            store.db.rollback()
            raise


def settle_attempt(store, attempt, actual, usage):
    if (budget := current_budget.get()) and (receipt := store.get('daily_reservation:' + str(attempt))):
        # Missing/invalid usage is uncertain, never a zero-cost request.
        if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                                              for key in ('input_tokens', 'output_tokens')):
            return
        budget.settle(receipt, actual)


def require_automatic():
    if automatic_request.get() and (budget := current_budget.get()) and not budget.settings()['enabled']:
        raise AutomaticStopped()
