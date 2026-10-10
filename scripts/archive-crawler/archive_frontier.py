"""Fair, resumable discovery from local Git objects, without a page inventory.

One small directory stack per host replaces a fixed recursive file shortlist.
The portal keeps these cursors in its main database, so campaigns (including an
older explicit resume) cannot rewind the frontier. Missing objects advance the
cursor too; only a completed pass with missing sources is revisited after a day.
"""

from contextlib import closing
import json
import re
import time
from pathlib import Path

from common import CrawlError, Store, candidate_exclusion, tier
from discovery import Archive, SKIP, record_source
from site_inventory import InventoryPause, SiteInventory

RETRY_SECONDS = 86400
TABLE = 'discovery_frontier'


def initialize(archive, seeds):
    db = archive.store.db
    db.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE}(
        host TEXT PRIMARY KEY, tree TEXT NOT NULL, category TEXT NOT NULL,
        priority INTEGER NOT NULL, cursor TEXT NOT NULL DEFAULT '[]',
        complete INTEGER NOT NULL DEFAULT 0, turn INTEGER NOT NULL DEFAULT 0,
        visited INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
        unavailable INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
        error TEXT)''')
    categories = {seed['host'].lower(): seed['category'] for seed in seeds}
    # Only the bounded website-root inventory, never recursive ls-tree or a walk.
    for host in db.execute('SELECT host,tree FROM hosts').fetchall():
        if candidate_exclusion('http://' + host['host'] + '/'):
            continue
        category = categories.get(host['host'].lower(), 'archive')
        db.execute(f'''INSERT INTO {TABLE}(host,tree,category,priority) VALUES (?,?,?,?)
            ON CONFLICT(host) DO UPDATE SET tree=excluded.tree, cursor='[]', complete=0,
                turn=0,visited=0,pages=0,unavailable=0,retry_at=0,error=NULL
            WHERE {TABLE}.tree != excluded.tree''',
            (host['host'], host['tree'], category, int(category == 'archive')))
    db.execute(f'DELETE FROM {TABLE} WHERE host NOT IN (SELECT host FROM hosts)')
    db.commit()
    sources = archive.env.get('GIT_ALTERNATE_OBJECT_DIRECTORIES', '')
    if archive.store.get('discovery_object_sources', '') != sources:
        db.execute(f'''UPDATE {TABLE} SET complete=0,cursor='[]',unavailable=0,retry_at=0,error=NULL
            WHERE unavailable>0 OR error IS NOT NULL''')
        archive.store.set('discovery_object_sources', sources)


def status(store, current_host=None):
    row = store.db.execute(f'''SELECT COUNT(*) hosts_total,
        COALESCE(SUM(turn>0),0) hosts_visited, COALESCE(SUM(complete),0) hosts_complete,
        COALESCE(SUM(pages),0) pages_scanned, COALESCE(SUM(unavailable),0) sources_unavailable,
        COALESCE(SUM(error IS NOT NULL),0) metadata_unavailable,
        COALESCE(SUM(complete=0 AND retry_at<=?),0) remaining
        FROM {TABLE}''', (time.time(),)).fetchone()
    return {**dict(row), 'current_host': current_host}


def ordered_tree(inventory, frame):
    cache = getattr(inventory, '_frontier_ordered', None)
    if cache is None:
        cache = inventory._frontier_ordered = {}
    identity = (frame['tree'], bool(frame['path']))
    if identity in cache:
        return cache[identity]
    entries = inventory.tree(frame['tree']).items()
    if not frame['path']:
        entries = [(name, entry) for name, entry in entries if entry[0] == b'40000' and tier(name)]
        key = lambda entry: (tier(entry[0]), entry[0])
    else:
        def key(entry):
            name = entry[0]
            rank = 0 if re.search(r'links?|guild|class|forum', name, re.I) else 1 if 'index' in name.lower() else 2
            return rank, name
    cache[identity] = sorted(entries, key=key)
    return cache[identity]


def next_page(inventory, cursor, allowance, deadline):
    """Advance only examined entries; a budget pause retains the next entry."""
    while cursor:
        if allowance[0] <= 0 or time.time() >= deadline:
            raise InventoryPause('slice_limit', 'Archive scan checkpoint saved')
        frame = cursor[-1]
        entries = ordered_tree(inventory, frame)
        if frame['index'] >= len(entries):
            cursor.pop()
            continue
        name, (mode, oid) = entries[frame['index']]
        frame['index'] += 1
        allowance[0] -= 1
        # Invalid archive names cannot become a source identity.
        if any(0xD800 <= ord(char) <= 0xDFFF for char in name) or name in ('.', '..'):
            continue
        path = frame['path'] + name
        if mode == b'40000':
            cursor.append({'tree': oid, 'path': path + '/', 'index': 0})
        elif mode in (b'100644', b'100755') and not SKIP.search(path):
            return path, oid
    return None


def scan(archive, args, *, deadline=None, notify=None, max_hosts=32, max_seconds=5):
    """One bounded slice. Rotating turns keep a huge/missing host from starving others."""
    initialize(archive, json.loads(Path(args.seeds).read_text()))
    store, db = archive.store, archive.store.db
    deadline = min(deadline or float('inf'), time.time() + max_seconds)
    # Cumulative per-slice metadata limits also bound unusually large trees.
    inventory = SiteInventory(archive, max_trees=512, max_bytes=16 * 1024 * 1024, max_seconds=max_seconds)
    allowance = [args.max_tree_entries]
    reads = probes = size = 0
    turn = db.execute(f'SELECT COALESCE(MAX(turn),0) FROM {TABLE}').fetchone()[0]
    # Complete passes with absent blobs can recover if the object store fills in.
    db.execute(f'''UPDATE {TABLE} SET complete=0,cursor='[]',unavailable=0,retry_at=0
        WHERE complete=1 AND unavailable>0 AND retry_at<=?''', (time.time(),))
    db.commit()
    hosts = db.execute(f'''SELECT * FROM {TABLE} WHERE complete=0 AND retry_at<=?
        ORDER BY turn,priority,host LIMIT ?''', (time.time(), max_hosts)).fetchall()
    try:
        for host in hosts:
            if (time.time() >= deadline or allowance[0] <= 0 or reads >= args.max_seed_captures
                    or probes >= args.max_seed_probes or size + args.max_page_bytes > args.max_seed_bytes):
                break
            cursor = json.loads(host['cursor']) or [{'tree': host['tree'], 'path': '', 'index': 0}]
            count = misses = host_probes = 0
            retry_at, error = 0, None
            slice_done = False
            before = allowance[0]
            if notify:
                notify(status(store, host['host']))
            try:
                while (count < args.max_per_seed and host_probes < 32 and reads < args.max_seed_captures
                       and probes < args.max_seed_probes and size + args.max_page_bytes <= args.max_seed_bytes):
                    page = next_page(inventory, cursor, allowance, deadline)
                    if page is None:
                        break
                    path, blob = page
                    source = 'websites/' + host['host'] + '/' + path
                    if db.execute('SELECT 1 FROM scans WHERE blob=? AND source=?', (blob, source)).fetchone():
                        continue
                    probes += 1
                    host_probes += 1
                    try:
                        data = archive.blob(blob, args.max_page_bytes)
                    except CrawlError:
                        misses += 1
                        continue
                    reads += 1
                    count += 1
                    size += len(data)
                    timestamp, _, relative = path.partition('/')
                    record_source(store, host['host'], timestamp, relative, blob, host['category'], data, archive.sha)
            except InventoryPause as pause:
                if not pause.retryable:
                    retry_at, error = time.time() + RETRY_SECONDS, pause.reason
                    inventory.close()
                else:
                    # A batch header may already have been read when the byte
                    # cap is hit. End this slice before reusing that stream.
                    slice_done = True
                # Fairly advance the turn even when this slice hits its bounds.
            except CrawlError:
                retry_at, error = time.time() + RETRY_SECONDS, 'metadata_unavailable'
                inventory.close()
            finally:
                turn += 1
                complete = not cursor
                if complete and host['unavailable'] + misses:
                    retry_at = time.time() + RETRY_SECONDS
                db.execute(f'''UPDATE {TABLE} SET cursor=?,complete=?,turn=?,visited=visited+?,
                    pages=pages+?,unavailable=unavailable+?,retry_at=?,error=? WHERE host=?''',
                    (json.dumps(cursor), complete, turn, before-allowance[0], count, misses, retry_at, error, host['host']))
                db.commit()
            if slice_done:
                break
    finally:
        inventory.close()
    summary = {**status(store), 'reads': reads, 'bytes': size, 'probes': probes}
    if notify:
        notify(summary)
    return summary


def advance(args, run, *, root, deadline, notify=None):
    """Keep cursors outside campaigns and copy only link metadata into this run."""
    with closing(Store(root)) as main:
        archive = Archive(args.archive_repo, main)
        summary = scan(archive, args, deadline=deadline, notify=notify)
        run.db.execute('ATTACH DATABASE ? AS frontier', (str(main.root / 'crawl.sqlite3'),))
        try:
            for table in ('scans', 'links', 'ezboard_aliases'):
                run.db.execute(f'INSERT OR IGNORE INTO {table} SELECT * FROM frontier.{table}')
            run.db.commit()
        finally:
            run.db.execute('DETACH DATABASE frontier')
        return summary
