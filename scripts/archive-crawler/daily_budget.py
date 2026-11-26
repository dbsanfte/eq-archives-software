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
from threading import Lock
import uuid

from common import CrawlError, now
from luna_limits import MINUTE_REQUEST_TARGET, MINUTE_TOKEN_TARGET

SCALE = 1_000_000_000
MAINTENANCE_SCOPE_LIMIT_USD = 200
PORTAL_PRIORITY = 'portal'
DEFAULT = {'enabled': False, 'configured': False, 'daily_usd': 2.0, 'monthly_usd': 500.0, 'site_usd': None,
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


def attempt_spending(store):
    """Settled usage plus only unresolved reservations in an operation ledger.

    Status describes validation, not payment. A rejected or corrected response
    still costs money, and a received response without usage remains uncertain.
    Keep original reservations/history intact; settlement replaces their charge.
    """
    actual = pending = 0
    # Round each request exactly as the shared integer ledger does. Rounding a
    # floating-point SUM instead can gain/lose nanodollars at the admission edge.
    for settled, reserved in store.db.execute('SELECT actual,reserved FROM attempts'):
        if settled is None:
            pending += units(reserved)
        else:
            actual += units(settled)
    return {'estimated_usd': actual / SCALE, 'reserved_usd': pending / SCALE}


def attempt_remaining_units(store, maximum):
    """Call under BEGIN IMMEDIATE for admission, sharing daily-ledger rounding."""
    return units(maximum) - sum(units(value) for value in attempt_spending(store).values())


def scope_charge(actual, reserved):
    """Project each row with the same rounding as the original audit path."""
    try:
        amount = units(reserved if actual is None else actual)
        if amount <= (1 << 63) - 1:
            return (0, amount, 0) if actual is None else (amount, 0, 0)
    except (CrawlError, ArithmeticError):
        pass
    return 0, 0, 1


def install_scope_totals(db):
    """Backfill an enrichment scope, then record changed IDs from any writer."""
    installed = "SELECT 1 FROM sqlite_schema WHERE name='scope_attempt_deleted' AND type='trigger'"
    if db.execute(installed).fetchone():
        return
    with db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute(installed).fetchone():
            return
        db.execute('''CREATE TABLE scope_attempt_totals (
            id INTEGER PRIMARY KEY CHECK(id=1), spent INTEGER NOT NULL,
            pending INTEGER NOT NULL, invalid INTEGER NOT NULL)''')
        db.execute('''CREATE TABLE scope_attempt_charges (
            id INTEGER PRIMARY KEY, spent INTEGER NOT NULL,
            pending INTEGER NOT NULL, invalid INTEGER NOT NULL)''')
        db.execute('CREATE TABLE scope_attempt_dirty (id INTEGER PRIMARY KEY)')
        totals = [0, 0, 0]

        def charges():
            for identifier, actual, reserved in db.execute('SELECT id,actual,reserved FROM attempts'):
                charge = scope_charge(actual, reserved)
                for index, part in enumerate(charge):
                    totals[index] += part
                yield (identifier, *charge)

        db.executemany('INSERT INTO scope_attempt_charges VALUES (?,?,?,?)', charges())
        if any(value >= 1 << 63 for value in totals):
            raise CrawlError('Invalid Luna spend accounting; paid work is paused')
        db.execute('INSERT INTO scope_attempt_totals VALUES (1,?,?,?)', totals)
        # Keep triggers usable from old images and standalone SQLite. Decimal
        # conversion happens on dirty rows only, inside the next admission.
        db.execute('''CREATE TRIGGER scope_attempt_inserted AFTER INSERT ON attempts BEGIN
            INSERT OR IGNORE INTO scope_attempt_dirty VALUES (NEW.id);
            END''')
        db.execute('''CREATE TRIGGER scope_attempt_changed AFTER UPDATE OF id,actual,reserved ON attempts
            WHEN OLD.id IS NOT NEW.id OR OLD.actual IS NOT NEW.actual OR OLD.reserved IS NOT NEW.reserved BEGIN
            INSERT OR IGNORE INTO scope_attempt_dirty VALUES (OLD.id), (NEW.id);
            END''')
        db.execute('''CREATE TRIGGER scope_attempt_deleted AFTER DELETE ON attempts BEGIN
            INSERT OR IGNORE INTO scope_attempt_dirty VALUES (OLD.id);
            END''')


def scope_spending_units(db):
    """Fold pending changes under the scope's existing write transaction."""
    if not db.in_transaction:
        raise CrawlError('Scope spend accounting requires a write transaction')
    totals = list(db.execute('SELECT spent,pending,invalid FROM scope_attempt_totals WHERE id=1').fetchone())
    changes = db.execute('''SELECT dirty.id,a.id,a.actual,a.reserved,p.spent,p.pending,p.invalid
        FROM scope_attempt_dirty dirty LEFT JOIN attempts a ON a.id=dirty.id
        LEFT JOIN scope_attempt_charges p ON p.id=dirty.id''').fetchall()
    for identifier, present, actual, reserved, old_spent, old_pending, old_invalid in changes:
        charge = scope_charge(actual, reserved) if present is not None else (0, 0, 0)
        for index, previous in enumerate((old_spent, old_pending, old_invalid)):
            totals[index] += charge[index] - (previous or 0)
        if present is None:
            db.execute('DELETE FROM scope_attempt_charges WHERE id=?', (identifier,))
        else:
            db.execute('INSERT OR REPLACE INTO scope_attempt_charges VALUES (?,?,?,?)', (identifier, *charge))
    if (any(type(value) is not int or not 0 <= value < 1 << 63 for value in totals)
            or totals[2]):
        raise CrawlError('Invalid Luna spend accounting; paid work is paused')
    if changes:
        db.execute('UPDATE scope_attempt_totals SET spent=?,pending=?,invalid=? WHERE id=1', totals)
        db.execute('DELETE FROM scope_attempt_dirty')
    return totals[0] + totals[1]


class DailyBudgetPause(CrawlError):
    def __init__(self, amount, retry_at, period='daily'):
        self.amount, self.retry_at, self.period = amount, retry_at, period
        super().__init__(f'{period.title()} Luna budget reached; paid work resumes after the UTC {period} reset')


class LunaRatePause(DailyBudgetPause):
    def __init__(self, until, *, status=None):
        self.amount, self.retry_at, self.period = 0, until.isoformat(), 'provider'
        self.status = status
        CrawlError.__init__(self, 'Luna capacity is low; paid work will retry automatically after the cooldown')


class SiteBudgetExceeded(CrawlError):
    """A lifetime site allowance is exhausted; calendar resets cannot renew it."""


class EnrichmentPriorityPause(CrawlError):
    def __init__(self):
        super().__init__('Archive enrichment has priority. Discovery grading resumes automatically afterward.')


class AutomaticStopped(CrawlError):
    def __init__(self):
        super().__init__('Automatic mode is paused; saved progress is retained')


class DailyBudget:
    def __init__(self, directory):
        self.root = Path(directory)
        self.observation_lock = Lock()
        self.observed_at = ''
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
                CREATE TABLE IF NOT EXISTS luna_capacity(id INTEGER PRIMARY KEY CHECK(id=1),
                    retry_at TEXT NOT NULL, observed_at TEXT NOT NULL, headroom TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS luna_daily_tokens(day TEXT PRIMARY KEY,
                    tokens INTEGER NOT NULL, charge INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS luna_recent_usage(slot INTEGER PRIMARY KEY,
                    second INTEGER NOT NULL, requests INTEGER NOT NULL, tokens INTEGER NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (json.dumps(DEFAULT),))
            self.install_totals(db)
        (self.root / 'budget.sqlite3').chmod(0o600)

    @staticmethod
    def install_totals(db):
        """Backfill once and include writes from workers using an older image."""
        installed = "SELECT 1 FROM sqlite_schema WHERE name='luna_total_delete' AND type='trigger'"
        if db.execute(installed).fetchone():
            return
        # Initialization above may have opened an implicit transaction.
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        if db.execute(installed).fetchone():
            return
        db.execute('''CREATE TABLE luna_day_totals(day TEXT PRIMARY KEY,
            spent INTEGER NOT NULL,pending INTEGER NOT NULL,invalid INTEGER NOT NULL)''')
        valid = "typeof({r}.reserved)='integer' AND {r}.reserved>=0 AND ({r}.actual IS NULL OR (typeof({r}.actual)='integer' AND {r}.actual>=0))"

        def contribution(alias):
            row = valid.format(r=alias)
            return (f'CASE WHEN {row} THEN COALESCE({alias}.actual,0) ELSE 0 END',
                    f'CASE WHEN {row} AND {alias}.actual IS NULL THEN {alias}.reserved ELSE 0 END', f'NOT ({row})')

        totals = ','.join('SUM('+field+')' for field in contribution('r'))
        db.execute(f'INSERT INTO luna_day_totals SELECT r.day,{totals} FROM reservations r GROUP BY r.day')
        add = f'''INSERT INTO luna_day_totals VALUES (NEW.day,{','.join(contribution('NEW'))})
            ON CONFLICT(day) DO UPDATE SET spent=spent+excluded.spent,pending=pending+excluded.pending,
            invalid=invalid+excluded.invalid;'''
        spent, pending, invalid = contribution('OLD')
        remove = f'''UPDATE luna_day_totals SET spent=spent-({spent}),pending=pending-({pending}),
            invalid=invalid-({invalid}) WHERE day=OLD.day;'''
        db.execute(f'CREATE TRIGGER luna_total_insert AFTER INSERT ON reservations BEGIN {add} END')
        db.execute(f'''CREATE TRIGGER luna_total_update AFTER UPDATE OF day,reserved,actual ON reservations
            WHEN OLD.day IS NOT NEW.day OR OLD.reserved IS NOT NEW.reserved OR OLD.actual IS NOT NEW.actual
            BEGIN {remove} {add} END''')
        db.execute(f'CREATE TRIGGER luna_total_delete AFTER DELETE ON reservations BEGIN {remove} END')

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
        if purpose not in ('discovery','enrichment','maintenance'):
            raise ValueError('Invalid Luna request purpose')
        token = current_budget.set(self)
        auto = automatic_request.set(automatic)
        scope = request_purpose.set(purpose)
        owner = owner or 'import-' + uuid.uuid4().hex
        claim=priority_owner.set(owner if purpose!='discovery' else None)
        try:
            if purpose == 'enrichment':self.prioritize(owner, True)
            yield self
        finally:
            try:
                if purpose != 'discovery':self.prioritize(owner, False)
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

    def needs_history_seed(self):
        # A configured calendar-month ledger was seeded before its settings
        # were saved. Later paid work already reserves/settles here directly.
        # Daily-only legacy settings still need the month-wide migration.
        with self.database() as db:
            saved = json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
        return not saved.get('configured') or 'monthly_usd' not in saved

    @staticmethod
    def validate(payload):
        from grading import criteria
        if (not isinstance(payload, dict) or set(payload)-{'monthly_usd', 'site_usd'} != {'enabled', 'daily_usd', 'min_grade', 'grading_criteria', 'revision'}
                or type(payload['enabled']) is not bool or type(payload['min_grade']) is not int
                or payload['min_grade'] not in range(4) or type(payload['revision']) is not int
                or not 0 < units(payload['daily_usd']) <= 1_000_000 * SCALE
                or not 0 < units(payload.get('monthly_usd',DEFAULT['monthly_usd'])) <= 1_000_000 * SCALE
                or 'site_usd' in payload and not 0 < units(payload['site_usd']) <= 1_000_000 * SCALE):
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
        row = db.execute('SELECT spent,pending,invalid FROM luna_day_totals WHERE day=?', (day,)).fetchone()
        if row and row[2]:
            raise CrawlError('Invalid Luna spend accounting; paid work is paused')
        return (row[0], row[1]) if row else (0, 0)

    @staticmethod
    def capacity(db, when, spent, pending):
        day = when.date().isoformat()
        reported = db.execute('SELECT tokens,charge FROM luna_daily_tokens WHERE day=?', (day,)).fetchone()
        tokens, charge = reported if reported else (0, 0)
        # Old images and unresolved requests have only a dollar receipt. At
        # Luna's least expensive metered rate ($0.10 per million), every 100
        # nano-dollars represent at most one token in the local accounting.
        tokens += (max(0, spent + pending - charge) + 99) // 100
        row = db.execute('SELECT retry_at FROM luna_capacity WHERE id=1').fetchone()
        retry_at = row[0] if row and row[0] > when.isoformat() else ''
        floor = int(when.timestamp()) - 59
        requests, recent_tokens = db.execute('''SELECT COALESCE(SUM(requests),0),COALESCE(SUM(tokens),0)
            FROM luna_recent_usage WHERE second>=?''', (floor,)).fetchone()
        if requests >= MINUTE_REQUEST_TARGET or recent_tokens >= MINUTE_TOKEN_TARGET:
            pending_requests, pending_tokens = requests, recent_tokens
            for second, count, total in db.execute('''SELECT second,requests,tokens
                    FROM luna_recent_usage WHERE second>=? ORDER BY second''', (floor,)):
                pending_requests -= count
                pending_tokens -= total
                if pending_requests < MINUTE_REQUEST_TARGET and pending_tokens < MINUTE_TOKEN_TARGET:
                    retry_at = max(retry_at, datetime.fromtimestamp(second + 60, timezone.utc).isoformat())
                    break
        return retry_at, tokens, requests, recent_tokens

    def check_luna_capacity(self):
        when = instant()
        with self.database() as db:
            db.execute('BEGIN')
            retry_at, *_ = self.capacity(db, when, *self.totals(db, when.date().isoformat()))
        if retry_at:
            raise LunaRatePause(datetime.fromisoformat(retry_at))

    def observe_luna_capacity(self, when, delay, headroom):
        """Keep the longest active cooldown and sample numeric provider headers."""
        retry_at = (when + timedelta(seconds=delay)).isoformat() if delay else ''
        sample_at = when.isoformat() if headroom else ''
        if not retry_at and not sample_at:
            return
        cutoff = (when - timedelta(seconds=5)).isoformat()
        with self.observation_lock:
            if not retry_at and self.observed_at > cutoff:
                return
            with self.database() as db:
                db.execute('''INSERT INTO luna_capacity VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    retry_at=MAX(luna_capacity.retry_at,excluded.retry_at),
                    observed_at=MAX(luna_capacity.observed_at,excluded.observed_at),
                    headroom=CASE WHEN excluded.observed_at>luna_capacity.observed_at THEN excluded.headroom
                        ELSE luna_capacity.headroom END
                    WHERE excluded.retry_at>luna_capacity.retry_at OR
                        (excluded.observed_at<>'' AND luna_capacity.observed_at<=?)''',
                    (retry_at, sample_at, json.dumps(headroom), cutoff))
            self.observed_at = max(self.observed_at, sample_at)

    @staticmethod
    def limits(db, when, config):
        day=when.date().isoformat();first=when.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        month_reset=(first+timedelta(days=32)).replace(day=1)
        month=db.execute('''SELECT COALESCE(SUM(spent),0),COALESCE(SUM(pending),0),COALESCE(SUM(invalid),0)
            FROM luna_day_totals WHERE day>=? AND day<?''',
            (first.date().isoformat(),month_reset.date().isoformat())).fetchone()
        if month[2]:
            raise CrawlError('Invalid Luna spend accounting; paid work is paused')
        spent,pending=DailyBudget.totals(db,day)
        daily=units(config['daily_usd'])-spent-pending
        monthly=units(config.get('monthly_usd',DEFAULT['monthly_usd']))-month[0]-month[1]
        retry_at,tokens,requests,recent_tokens=DailyBudget.capacity(db,when,spent,pending)
        day_reset=(when+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        period='monthly' if monthly<daily else 'daily'
        return {'estimated_usd':spent/SCALE,'unresolved_usd':pending/SCALE,
                'luna_retry_at':retry_at,'luna_accounted_tokens_today':tokens,
                'luna_recent_requests':requests,'luna_recent_tokens':recent_tokens,
                '_remaining_units':max(0,min(daily,monthly)),
                'remaining_usd':max(0,min(daily,monthly))/SCALE,'budget_period':period,
                'resets_at':(month_reset if period=='monthly' else day_reset).isoformat(),
                'daily_remaining_usd':max(0,daily)/SCALE,'daily_resets_at':day_reset.isoformat(),
                'monthly':{'estimated_usd':month[0]/SCALE,'unresolved_usd':month[1]/SCALE,
                           'remaining_usd':max(0,monthly)/SCALE,'resets_at':month_reset.isoformat()}}

    @staticmethod
    def _enrichment_pending(db, when, *, owner='', maintenance=False):
        # The portal claim reserves priority over discovery. A committed import
        # also has its own lease, which historical maintenance must respect.
        queue = PORTAL_PRIORITY if maintenance else owner
        return db.execute('SELECT 1 FROM enrichment_priority WHERE expires>? AND owner NOT IN (?,?) LIMIT 1',
                          (when.isoformat(), owner, queue)).fetchone() is not None

    def snapshot(self, *, maintenance=False):
        when = instant()
        with self.database() as db:
            db.execute('BEGIN')
            config = {**DEFAULT,**json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])}
            limits=self.limits(db,when,config)
            priority = self._enrichment_pending(db, when, maintenance=maintenance)
        return {'settings': config, 'complete': config['configured'], **{k:v for k,v in limits.items() if not k.startswith('_')},
                'enrichment_pending':priority,'day': when.date().isoformat(), 'timezone': 'UTC'}

    def reserve(self, amount, *, site_spent_units=None, site_maximum=None):
        reserved = units(amount)
        if (site_spent_units is None) != (site_maximum is None):
            raise CrawlError('Invalid site spend accounting; paid work is paused')
        if site_spent_units is not None:
            if type(site_spent_units) is not int or site_spent_units < 0 or not units(site_maximum):
                raise CrawlError('Invalid site spend accounting; paid work is paused')
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            when = instant()
            day = when.date().isoformat()
            config = json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
            purpose = request_purpose.get()
            if purpose == 'maintenance' and not config.get('configured'):
                raise AutomaticStopped()
            if automatic_request.get() and not config['enabled']:
                raise AutomaticStopped()
            if site_spent_units is not None:
                if purpose == 'maintenance':
                    maximum = MAINTENANCE_SCOPE_LIMIT_USD
                else:
                    maximum = config.get('site_usd') if config['configured'] else None
                    maximum = site_maximum if maximum is None else maximum
                if site_spent_units + reserved > units(maximum):
                    raise SiteBudgetExceeded('Site Luna budget reached; paid results are retained')
            limits=self.limits(db,when,config)
            if config['configured'] and reserved>limits['_remaining_units']:
                raise DailyBudgetPause(amount, limits['resets_at'], limits['budget_period'])
            if limits['luna_retry_at']:
                raise LunaRatePause(datetime.fromisoformat(limits['luna_retry_at']))
            if purpose in ('discovery','maintenance') and self._enrichment_pending(
                    db, when, owner=priority_owner.get() or '', maintenance=purpose=='maintenance'):
                raise EnrichmentPriorityPause()
            if owner:=priority_owner.get():
                db.execute('INSERT OR REPLACE INTO enrichment_priority VALUES (?,?)',
                           (owner,(when+timedelta(minutes=5)).isoformat()))
            receipt = uuid.uuid4().hex
            db.execute('INSERT INTO reservations VALUES (?,?,?,NULL,?)', (receipt, day, reserved, when.isoformat()))
            second = int(when.timestamp())
            db.execute('''INSERT INTO luna_recent_usage VALUES (?,?,1,0) ON CONFLICT(slot) DO UPDATE SET
                requests=CASE WHEN second=excluded.second THEN requests+1 ELSE 1 END,
                tokens=CASE WHEN second=excluded.second THEN tokens ELSE 0 END,
                second=excluded.second''', (second % 120, second))
        return receipt

    def settle(self, receipt, actual, *, usage=None):
        actual = units(actual)
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT actual,day,created FROM reservations WHERE id=?', (receipt,)).fetchone()
            if previous is None or previous[0] is not None and previous[0] != actual:
                raise CrawlError('Luna reservation accounting changed; paid work is paused')
            db.execute('UPDATE reservations SET actual=? WHERE id=?', (actual, receipt))
            if (previous[0] is None and isinstance(usage, dict) and
                    all(type(usage.get(key)) is int and 0 <= usage[key] <= 10**9
                        for key in ('input_tokens', 'output_tokens'))):
                tokens = usage['input_tokens'] + usage['output_tokens']
                db.execute('''INSERT INTO luna_daily_tokens VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET
                    tokens=luna_daily_tokens.tokens+excluded.tokens, charge=luna_daily_tokens.charge+excluded.charge''',
                    (previous[1], tokens, actual))
                second = int(datetime.fromisoformat(previous[2]).timestamp())
                db.execute('UPDATE luna_recent_usage SET tokens=tokens+? WHERE slot=? AND second=?',
                           (tokens, second % 120, second))

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


def reserve_attempt(store, attempt, amount, *, site_maximum=None, scope_totals=False):
    """Call while holding the operation's write transaction, before commit/send."""
    try:
        site_spent = None
        if site_maximum is not None:
            # The just-inserted attempt is already included in this locked
            # ledger. Count it exactly once, using the shared integer rounding.
            spent = scope_spending_units(store.db) if scope_totals else sum(units(v) for v in attempt_spending(store).values())
            site_spent = spent - units(amount)
            if site_spent < 0:
                raise CrawlError('Invalid site spend accounting; paid work is paused')
        if budget := current_budget.get():
            receipt = (budget.reserve(amount) if site_spent is None else
                       budget.reserve(amount, site_spent_units=site_spent, site_maximum=site_maximum))
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                             ('daily_reservation:' + str(attempt), json.dumps(receipt)))
        elif site_spent is not None and site_spent + units(amount) > units(site_maximum):
            raise SiteBudgetExceeded('Site Luna budget reached; paid results are retained')
    except BaseException:
        store.db.rollback()
        raise


def settle_attempt(store, attempt, actual, usage):
    if (budget := current_budget.get()) and (receipt := store.get('daily_reservation:' + str(attempt))):
        # Missing/invalid usage is uncertain, never a zero-cost request.
        if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                                              for key in ('input_tokens', 'output_tokens')):
            return
        budget.settle(receipt, actual, usage=usage)


def require_automatic():
    if automatic_request.get() and (budget := current_budget.get()) and not budget.settings()['enabled']:
        raise AutomaticStopped()
