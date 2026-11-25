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
ESTIMATE = f"""LEFT JOIN capture_size_estimates sz ON sz.candidate=c.id
    AND sz.scope=c.scope AND sz.mode={MODE}"""
PAGES = f"COALESCE(sz.pages,CASE WHEN {MODE}='page' THEN 1 ELSE 0 END)"

PROBE_INTERVAL = 15
PROBE_SECONDS = 12
RETRY_DELAY = timedelta(hours=1)


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


def measure_next(root, transport):
    """Spend one shared Wayback turn without retaining a portal DB lock."""
    from state import connect

    at = now()
    with connect(root) as store:
        row = store.db.execute(f"""SELECT c.id,c.scope,c.captures,{MODE} mode,
            sz.pages,sz.resume_key,sz.last_url FROM candidates c
            {ESTIMATE} LEFT JOIN capture_queue_failures f ON f.candidate=c.id AND f.decision=c.decision
            WHERE c.state IN ('approved_waiting_batch','capture_resume_queued') AND f.candidate IS NULL
            AND c.scope IS NOT NULL AND {MODE}!='page'
            AND (sz.candidate IS NULL OR sz.retry_at<=?)
            ORDER BY {PRIORITY} DESC,(sz.pages IS NOT NULL),sz.retry_at,c.rowid DESC LIMIT 1""", (at,)).fetchone()
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
