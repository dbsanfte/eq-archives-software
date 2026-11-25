"""Rank approved scopes using saved categories and bounded Wayback page counts."""

import json
from datetime import datetime, timedelta
from time import monotonic
from urllib.parse import urlsplit

from common import CAPTURE_WINDOW, CrawlError, now, original_url, within_capture_scope


MODE = "COALESCE(json_extract(c.coverage,'$.scope_mode'),'directory')"
REFERENCE_TITLES = ('item database', 'item lookup', 'item search', 'item catalog',
                    'bestiary', 'npc database', 'monster database', 'creature database')
TITLE_MATCH = ' OR '.join(f"instr(lower(json_extract(value,'$.title')),'{title}')>0"
                          for title in REFERENCE_TITLES)
PRIORITY = f"""CASE WHEN {MODE} IN ('ezboard','sitepowerup')
    OR json_extract(c.rating,'$.category') IN ('forum','news','aggregator','bestiary','item_database')
    OR EXISTS (SELECT 1 FROM json_each(c.captures) WHERE
        COALESCE(json_extract(value,'$.selection_role'),'')!='site_home' AND ({TITLE_MATCH}))
    THEN 1 ELSE 0 END"""
ESTIMATE = """LEFT JOIN capture_size_estimates sz ON sz.candidate=r.id
    AND sz.scope=r.scope AND sz.mode=r.mode"""
PAGES = "COALESCE(sz.pages,CASE WHEN r.mode='page' THEN 1 ELSE 0 END)"
ELIGIBLE_STATES = "('approved_waiting_batch','capture_resume_queued')"
RANK_ROWS = f"""SELECT c.id,c.state,c.rowid,c.scope,{MODE},c.decision,
    COALESCE(json_extract(c.decision,'$.capture_after'),json_extract(c.decision,'$.reviewed_at')),
    {PRIORITY},COALESCE(NULLIF(json_extract(c.captures,'$[0].tier'),0),3)
    FROM candidates c WHERE c.state IN {ELIGIBLE_STATES}"""

PROBE_INTERVAL = 15
PROBE_SECONDS = 12
RETRY_DELAY = timedelta(hours=1)


def install_queue_ranks(db):
    """Keep hot rank reads off the candidate rows that hold source metadata."""
    installed = "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND name='capture_rank_update'"
    if db.execute(installed).fetchone():
        return
    with db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute(installed).fetchone():
            return
        db.execute('''CREATE TABLE capture_queue_ranks(id TEXT PRIMARY KEY,state TEXT NOT NULL,
            ordinal INTEGER NOT NULL,scope TEXT,mode TEXT NOT NULL,decision TEXT,ready TEXT,
            priority_tier INTEGER NOT NULL,date_tier INTEGER NOT NULL)''')
        db.execute('CREATE INDEX capture_queue_rank_state ON capture_queue_ranks(state)')
        db.execute("""CREATE INDEX capture_resumed_operations ON operations(updated)
            WHERE kind='capture' AND state='resume_queued'""")
        db.execute(f'INSERT INTO capture_queue_ranks {RANK_ROWS}')
        db.execute(f'''CREATE TRIGGER capture_rank_insert AFTER INSERT ON candidates
            WHEN NEW.state IN {ELIGIBLE_STATES}
                OR EXISTS(SELECT 1 FROM capture_queue_ranks WHERE id=NEW.id) BEGIN
                DELETE FROM capture_queue_ranks WHERE id=NEW.id AND NEW.state NOT IN {ELIGIBLE_STATES};
                INSERT OR REPLACE INTO capture_queue_ranks {RANK_ROWS} AND c.id=NEW.id;
            END''')
        db.execute(f'''CREATE TRIGGER capture_rank_update
            AFTER UPDATE OF state,scope,decision,coverage,captures,rating,id ON candidates
            WHEN OLD.state IN {ELIGIBLE_STATES} OR NEW.state IN {ELIGIBLE_STATES} BEGIN
                DELETE FROM capture_queue_ranks WHERE id=OLD.id AND
                    (NEW.state NOT IN {ELIGIBLE_STATES} OR OLD.id!=NEW.id);
                INSERT OR REPLACE INTO capture_queue_ranks {RANK_ROWS} AND c.id=NEW.id;
            END''')
        db.execute(f'''CREATE TRIGGER capture_rank_delete AFTER DELETE ON candidates
            WHEN OLD.state IN {ELIGIBLE_STATES} BEGIN
                DELETE FROM capture_queue_ranks WHERE id=OLD.id;
            END''')


def estimate_query(store, row):
    scope, mode = row['scope'], row['mode']
    if original_url(scope) != scope:
        raise CrawlError('Capture size needs a valid original scope')
    if mode == 'ezboard':
        from ezboard import address, board_name, catalog_url

        board = board_name(scope)
        forums = {item['token'] for item in store.db.execute(
            "SELECT token FROM ezboard_aliases WHERE board=? AND token LIKE 'f%'", (board,))}
        if not forums:
            raise CrawlError('Capture size needs source-proven Ezboard forums')
        url = catalog_url(board, {'host': urlsplit(scope).netloc, 'kind': 'f'})

        def member(value):
            item = address(value)
            return bool(item and item['host'] == urlsplit(scope).netloc and item['token'] in forums)

        return {'op': 'ezboard_list', 'url': url}, member
    if mode == 'sitepowerup':
        from sitepowerup import address, board_name, candidate, catalog_url

        board = board_name(scope)
        # Use the catalog spelling of the first successfully staged board page.
        sample = next((parsed for capture in json.loads(row['captures'] or '[]')
                       if (parsed := address(capture['url'])) and parsed['board'] == board), None)
        kind = sample['action'] if sample and sample['action'] in ('display', 'reply') else 'default'
        url = catalog_url(board, {'host': urlsplit(scope).netloc, 'kind': kind})
        return {'op': 'sitepowerup_list', 'url': url}, lambda value: candidate(value, board) is not None
    match = 'prefix' if scope.endswith('/') and within_capture_scope(scope + 'page.html', scope) else 'exact'
    return {'op': 'scope_list', 'url': scope, 'match': match}, lambda value: within_capture_scope(value, scope)


def measure_next(root, transport, *, prefer_continuation=False):
    """Spend one shared Wayback turn without retaining a portal DB lock."""
    from state import connect

    at = now()
    with connect(root) as store:
        row = store.db.execute(f"""SELECT pending.*,c.captures FROM (
            SELECT r.id,r.scope,r.mode,sz.pages,sz.resume_key,sz.last_url FROM capture_queue_ranks r
            {ESTIMATE} LEFT JOIN capture_queue_failures f ON f.candidate=r.id AND f.decision=r.decision
            WHERE f.candidate IS NULL AND r.scope IS NOT NULL AND r.mode!='page'
            AND (sz.candidate IS NULL OR sz.retry_at<=?)
            ORDER BY r.priority_tier DESC,((sz.resume_key IS NOT NULL)=?) DESC,sz.retry_at,r.ordinal DESC LIMIT 1
            ) pending JOIN candidates c ON c.id=pending.id""", (at,prefer_continuation)).fetchone()
        if not row:
            return
        try:
            query, member = estimate_query(store, row)
        except CrawlError:
            query = None
    pages, resume_key, last_url = row['pages'], row['resume_key'], row['last_url']
    retry_at = (datetime.fromisoformat(at) + RETRY_DELAY).isoformat()
    if query:
        try:
            with transport.turn(monotonic() + PROBE_SECONDS):
                response = transport.request({**query, 'estimate': True, 'resume_key': resume_key,
                    'from': CAPTURE_WINDOW['from'], 'to': CAPTURE_WINDOW['to'],
                    'transport': {'max_requests': 1, 'max_seconds': PROBE_SECONDS,
                                  'max_response_bytes': 1048576, 'max_total_bytes': 1048576}})
            if response['ok']:
                result = response['result']
                following = result['resume_key']
                if following and following == resume_key:
                    raise CrawlError('Wayback repeated the size estimate continuation')
                urls = [value for item in result['captures'] if (value := original_url(item['url'])) and member(value)]
                pages = (pages or 0) + len(set(urls) - {last_url})
                if urls:
                    last_url = urls[-1]
                resume_key = following
                retry_at = at if following else None
        except CrawlError:
            pass  # The next claim still has a rank even without Wayback.
    with connect(root) as store:
        store.db.execute(f"""INSERT OR REPLACE INTO capture_size_estimates
            (candidate,scope,mode,pages,resume_key,last_url,retry_at)
            SELECT c.id,c.scope,{MODE},?,?,?,? FROM candidates c WHERE c.id=? AND c.scope=? AND {MODE}=?
              AND c.state IN ('approved_waiting_batch','capture_resume_queued')""",
            (pages, resume_key, last_url, retry_at, row['id'], row['scope'], row['mode']))
        store.db.commit()
