"""Resumable, all-file CDX inventories for newly approved captures.

SQLite checkpoints each catalog page and each exact URL/date. No archive walk,
HTML-only spider, digest collapse, file-count ceiling or growing JSON rewrite.
Runtime limits pause acquisition; only an exhausted inventory reaches Review.
"""
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
from urllib.parse import urlsplit

from archive_layout import archive_path
from common import CAPTURE_WINDOW, CrawlError, Store, digest, in_capture_window, now, original_url, save, within_capture_scope
from indexer.capture_enrichment import DEFAULT_POLICY

POLICY = 'complete-files-v1'
# Cumulative transport safeguards, deliberately large enough for thousands of
# files. Explicit resume can extend an exhausted allowance, retaining all usage.
ALLOWANCE = {'requests': 100000, 'bytes': 50 * 1024**3, 'seconds': 7 * 86400}
DISK_RESERVE = 256 * 1024**2
FILE_UNAVAILABLE = {'Wayback HTTP 403', 'Wayback HTTP 404', 'Wayback HTTP 410'}


def seed_retry(root, store, retained):
    """Copy only private catalog metadata; successful sources stay in place.

    The caller commits this together with the new plan. Repeated retries never
    alter an earlier review, reset transport usage, or re-download its files.
    """
    from state import valid_id
    directory = Path(root).resolve() / 'batches' / valid_id(retained.get('capture_batch_id', retained['batch_id'])) / 'complete'
    site = retained['sites'][0]['id']
    try:
        checkpoint = json.loads((directory / 'manifest.json').read_text())
        if digest(checkpoint) != retained.get('capture_manifest_sha256', digest(retained)):
            raise CrawlError('Completed capture checkpoint changed; failed files cannot be retried')
        source = sqlite3.connect((directory / 'crawl.sqlite3').as_uri() + '?mode=ro', uri=True)
        try:
            source.row_factory = sqlite3.Row
            captures = [json.loads(row[0]) for row in source.execute("SELECT capture FROM full_records WHERE site=? AND state='captured' ORDER BY rowid", (site,))]
            if captures != retained['captures']:
                raise CrawlError('Completed file inventory changed; failed files cannot be retried')
            for table in ('full_queries', 'full_records', 'full_dependencies', 'full_scanned'):
                restriction = "id IN (SELECT id FROM full_records WHERE site=?)" if table == 'full_scanned' else 'site=?'
                rows = source.execute('SELECT * FROM ' + table + ' WHERE ' + restriction, (site,))
                columns = [item[0] for item in rows.description]
                store.db.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", rows)
        finally:
            source.close()
    except (OSError, ValueError, sqlite3.Error) as error:
        raise CrawlError('Completed capture checkpoint is unavailable; saved files are retained') from error
    store.db.execute("UPDATE full_records SET state='pending',note=NULL WHERE state='unavailable'")
    store.db.execute("""UPDATE full_queries SET done=0,note=NULL WHERE note IS NOT NULL OR
        (found=0 AND EXISTS (SELECT 1 FROM full_dependencies d WHERE d.url=full_queries.url AND d.site=full_queries.site))""")


def board_limits(legacy):
    return {**legacy, 'max_captures': 1000000, 'max_catalog_rows': 10000000,
            'max_requests': ALLOWANCE['requests'], 'max_bytes': ALLOWANCE['bytes'],
            'max_seconds': ALLOWANCE['seconds'], 'max_page_bytes': 32 * 1024**2}


def configure_board(runner, fresh):
    """Opt only newly claimed portal work into complete captures, never old jobs."""
    if fresh:
        runner.config['complete_files'] = True
        runner.store.set(runner.name + '_config', runner.config)
    if runner.config.get('complete_files'):
        usage = runner.store.get('wayback_transport', {})
        stopped = runner.store.get(runner.name + '_status', {})
        exhausted = stopped.get('state') == 'bounded' and 'budget' in stopped.get('reason', '').lower()
        changes = {'max_' + key: max(usage.get(key, 0), runner.config['limits']['max_' + key]) + ALLOWANCE[key] for key in ALLOWANCE
                   if exhausted or usage.get(key, 0) >= runner.config['limits']['max_' + key]}
        for field, used in [('max_captures', sum(runner.status()['counts'].values())),
                            ('max_catalog_rows', runner.store.get(runner.name + '_catalog_rows', 0))]:
            if used >= runner.config['limits'][field]:
                changes[field] = used + board_limits(runner.config['limits'])[field]
        if changes:
            runner.extend(changes)


def allowed(url, site):
    if site['scope_mode'] in ('ezboard', 'sitepowerup'):
        return False  # these require the separate source-verified board engine
    value = original_url(url)
    if not value:
        return False
    if site['scope_mode'] == 'page':
        return value == original_url(site['url'])
    return within_capture_scope(value, site['scope'])


def capture(root, batch_id, sites, downloader_factory, progress=None, base_manifest=None):
    from captures import check_manifest, describe_source, verified_path
    retained = None
    if any(site.get('continued_from') for site in sites):
        from capture_continuation import retained_manifest
        if len(sites) != 1 or base_manifest:
            raise CrawlError('Continue each incomplete ordinary site independently')
        retained = retained_manifest(root, sites[0])
    root = Path(root)
    directory = root / 'batches' / batch_id / 'complete'
    store = Store(directory)
    db = store.db
    downloader = None
    signature = digest(sites)
    try:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS full_queries (
            id TEXT PRIMARY KEY, site TEXT NOT NULL, url TEXT NOT NULL, match TEXT NOT NULL,
            resume TEXT, done INTEGER NOT NULL DEFAULT 0, found INTEGER NOT NULL DEFAULT 0, note TEXT);
          CREATE TABLE IF NOT EXISTS full_records (
            id TEXT PRIMARY KEY, site TEXT NOT NULL, url TEXT NOT NULL, timestamp TEXT NOT NULL,
            record TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', capture TEXT,
            archive_path TEXT UNIQUE, note TEXT);
          CREATE INDEX IF NOT EXISTS full_pending ON full_records(state);
          CREATE TABLE IF NOT EXISTS full_dependencies (url TEXT, site TEXT, parent TEXT NOT NULL, PRIMARY KEY(url,site));
          CREATE TABLE IF NOT EXISTS full_scanned (id TEXT PRIMARY KEY);
        ''')
        if 'note' not in {row['name'] for row in db.execute('PRAGMA table_info(full_queries)')}:
            db.execute('ALTER TABLE full_queries ADD COLUMN note TEXT')
            db.commit()
        config = store.get('complete_config')
        if config and config['signature'] != signature:
            raise CrawlError('Approved capture scope or evidence changed after acquisition started')
        if not config:
            config = {'signature': signature, 'created_at': now(), 'limits': dict(ALLOWANCE)}
            retrying = retained and sites[0].get('continued_from', {}).get('mode') == 'retry_failed'
            if retrying:
                seed_retry(root, store, retained)
                config['limits'] = dict(retained['limits'])
            if base_manifest or retained:
                # Commit usage with the initial plan, without a separate commit
                # that could reset later usage following interrupted setup.
                db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                           ('wayback_transport', json.dumps((base_manifest or retained).get('transport', {}))))
            for site in ([] if base_manifest or retrying else sites):
                scope = urlsplit(site['scope'])
                match = 'exact' if site['scope_mode'] == 'page' or scope.query or not scope.path.endswith('/') else 'prefix'
                url = site['url'] if site['scope_mode'] == 'page' else site['scope']
                if not allowed(url.rstrip('/') + '/__capture_scope_probe__', site):
                    match = 'exact'  # unidentified shared-host roots cannot scan every account
                if match == 'prefix' and scope.path != '/':
                    url = url.rstrip('/')  # include the directory's extensionless index; filter /eq2 below
                db.execute('INSERT INTO full_queries(id,site,url,match) VALUES (?,?,?,?)',
                           (digest([site['id'], url]), site['id'], url, match))
            # Commit the plan and initial queries together. A crash must never
            # leave a configured run with no inventory query to resume.
            store.set('complete_config', config)
        result_path = directory / 'manifest.json'
        if result_path.exists():
            result = json.loads(result_path.read_text())
            check_manifest(root, result)
            return result
        used = store.get('wayback_transport', {})
        # Invocation after a pause is always an explicit /api/resume. No worker
        # retry renews these limits, and the ledger is never reset.
        extended = [key for key in ALLOWANCE if store.get('extend_transport_on_resume') or used.get(key, 0) >= config['limits'][key]]
        if extended:
            for key in extended:
                config['limits'][key] = max(used.get(key, 0), config['limits'][key]) + ALLOWANCE[key]
            store.set('complete_config', config)
            store.set('extend_transport_on_resume', False)
        site_map = {site['id']: site for site in sites}
        dependencies = {(r['url'], r['site']): json.loads(r['parent']) for r in db.execute('SELECT * FROM full_dependencies')}
        def record_key(site, url, stamp):
            return digest([site, original_url(url), stamp])
        def commit_capture(capture, record_id):
            verified_path(root, capture)
            path = archive_path(capture)
            old = db.execute('SELECT capture FROM full_records WHERE archive_path=? AND id!=?', (path, record_id)).fetchone()
            if old:
                raise CrawlError('Exact sources collide in the archive filename convention; capture paused without overwriting files')
            capture = {**capture, 'archive_path': path, **describe_source(root, capture)}
            db.execute("UPDATE full_records SET state='captured',capture=?,archive_path=?,note=NULL WHERE id=?",
                       (json.dumps(capture), path, record_id))
            db.commit()
        for site in sites:
            seeds = base_manifest or retained
            sources = [c for c in seeds['captures'] if c['candidate_id'] == site['id']] if seeds else site['captures']
            for source in sources:
                if not in_capture_window(source['timestamp']) or not (base_manifest or retained and retained.get('capture_policy') == POLICY or allowed(source['url'], site)):
                    continue
                key = record_key(site['id'], source['url'], source['timestamp'])
                if not db.execute('SELECT 1 FROM full_records WHERE id=?', (key,)).fetchone():
                    db.execute('INSERT INTO full_records(id,site,url,timestamp,record) VALUES (?,?,?,?,?)',
                               (key, site['id'], source['url'], source['timestamp'], '{}'))
                    commit_capture({**source, 'candidate_id': site['id']}, key)
        def report(phase, url=None):
            if progress:
                counts = {row['state']: row['n'] for row in db.execute('SELECT state,COUNT(*) n FROM full_records GROUP BY state')}
                size = db.execute("SELECT COALESCE(SUM(json_extract(capture,'$.bytes')),0) FROM full_records WHERE state='captured'").fetchone()[0]
                progress({'phase': phase, 'files': counts.get('captured', 0), 'bytes': size,
                          'versions_found': sum(counts.values()), 'versions_pending': counts.get('pending', 0),
                          'unavailable': counts.get('unavailable', 0),
                          'failed_lookups': db.execute('SELECT COUNT(*) FROM full_queries WHERE note IS NOT NULL').fetchone()[0],
                          'catalogs_pending': db.execute('SELECT COUNT(*) FROM full_queries WHERE done=0').fetchone()[0],
                          'urls_checked': counts.get('captured', 0) + counts.get('unavailable', 0),
                          'sites_total': len(sites), 'sites_done': len(sites) if phase == 'ready_for_review' else 0,
                          'site_url': sites[0]['url'], 'current_url': url, 'capture_window': dict(CAPTURE_WINDOW),
                          'capture_policy': POLICY, 'transport': store.get('wayback_transport', {})})
        def acquire(job):
            nonlocal downloader
            if shutil.disk_usage(directory).free <= DISK_RESERVE:
                raise CrawlError('Staging disk is nearly full; free space and resume. Completed files are retained.')
            if downloader is None:
                limits = config['limits']
                args = SimpleNamespace(delay=3, bytes_per_second=131072, max_requests=limits['requests'],
                    max_bytes=limits['bytes'], max_seconds=limits['seconds'],
                    max_page_bytes=min(limits['bytes'], shutil.disk_usage(directory).free - DISK_RESERVE))
                downloader = downloader_factory(store, args)
            if job['op'] == 'capture_file':
                job = {**job, 'max_file_bytes': shutil.disk_usage(directory).free - DISK_RESERVE}
            return downloader.call(job)
        def discover_assets():
            from source_assets import references
            for row in db.execute("SELECT id,site,capture FROM full_records WHERE state='captured' AND id NOT IN (SELECT id FROM full_scanned)").fetchall():
                parent = json.loads(row['capture'])
                evidence = {key: parent[key] for key in ('url', 'timestamp', 'sha256')}
                for url in references(root, parent):
                    if allowed(url, site_map[row['site']]):
                        continue  # the whole-scope inventory already covers this file
                    db.execute('INSERT OR IGNORE INTO full_dependencies(url,site,parent) VALUES (?,?,?)',
                               (url, row['site'], json.dumps(evidence)))
                    dependencies.setdefault((url, row['site']), evidence)
                    db.execute('INSERT OR IGNORE INTO full_queries(id,site,url,match) VALUES (?,?,?,?)',
                               (digest([row['site'], url]), row['site'], url, 'exact'))
                db.execute('INSERT INTO full_scanned VALUES (?)', (row['id'],))
                db.commit()
        report('preparing')
        # Finish paginated catalogs before downloading. The durable inventory
        # provides an honest total even when acquisition later pauses.
        def catalogs():
            for query in db.execute('SELECT * FROM full_queries WHERE done=0 ORDER BY rowid').fetchall():
                resume = query['resume']
                while True:
                    report('checking_wayback', query['url'])
                    try:
                        listing = acquire({'op': 'scope_list', 'url': query['url'], 'match': query['match'],
                                           **CAPTURE_WINDOW, 'resume_key': resume})
                    except CrawlError as error:
                        # An excluded external counter/image is an individual
                        # supporting-file gap, never grounds to abandon the site.
                        # The primary scope inventory and service failures still
                        # pause: they cannot establish complete site coverage.
                        if (str(error) not in FILE_UNAVAILABLE or query['match'] != 'exact'
                                or (query['url'], query['site']) not in dependencies):
                            raise
                        db.execute('UPDATE full_queries SET done=1,note=? WHERE id=?', (str(error), query['id']))
                        db.commit()
                        break
                    continuation = listing.get('resume_key')
                    if continuation and continuation == resume:
                        raise CrawlError('Wayback repeated its catalog continuation; full coverage remains unresolved')
                    found = 0
                    for record in listing['captures']:
                        if not in_capture_window(record['timestamp']):
                            raise CrawlError('Wayback inventory returned a version outside 1999–2006')
                        dependency = dependencies.get((original_url(record['url']), query['site']))
                        if not allowed(record['url'], site_map[query['site']]) and not (
                                dependency and original_url(record['url']) == query['url']):
                            continue
                        if dependency:
                            record = {**record, 'supporting_source': dependency}
                        found += 1
                        db.execute('INSERT OR IGNORE INTO full_records(id,site,url,timestamp,record) VALUES (?,?,?,?,?)',
                                   (record_key(query['site'], record['url'], record['timestamp']), query['site'], record['url'],
                                    record['timestamp'], json.dumps(record)))
                    db.execute('UPDATE full_queries SET resume=?,done=?,found=found+? WHERE id=?', (continuation, not continuation, found, query['id']))
                    db.commit()
                    if not continuation:
                        break
                    resume = continuation
        folder = directory / 'files'
        folder.mkdir(exist_ok=True, mode=0o700)
        while True:
            discover_assets()
            catalogs()
            row = db.execute("SELECT * FROM full_records WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                break
            report('downloading', row['url'])
            record = json.loads(row['record'])
            destination = folder / row['id']
            receipt = destination.with_suffix('.json')
            try:
                if receipt.exists():
                    captured = json.loads(receipt.read_text())
                else:
                    captured = acquire({'op': 'capture_file', 'url': row['url'], 'timestamp': row['timestamp'],
                                        **CAPTURE_WINDOW, 'destination': str(destination)})
                    captured.update(path=str(destination.relative_to(root)), candidate_id=row['site'], source='wayback',
                                    cdx_digest=record.get('digest'), cdx_length=record.get('length'),
                                    mimetype=record.get('mimetype'), retrieved_at=now(), site_coverage=POLICY)
                    if record.get('supporting_source'):
                        captured['supporting_source'] = record['supporting_source']
                    save(receipt, captured)
                if captured['url'] != row['url'] or captured['timestamp'] != row['timestamp']:
                    raise CrawlError('Downloaded file did not retain its exact catalog URL and date')
                commit_capture(captured, row['id'])
            except CrawlError as error:
                if str(error) not in FILE_UNAVAILABLE and not str(error).startswith(
                        ('Replay returned a different original URL', 'Replay returned a different dated version')):
                    raise
                db.execute("UPDATE full_records SET state='unavailable',note=? WHERE id=?", (str(error), row['id']))
                db.commit()
        captures = [json.loads(row[0]) for row in db.execute("SELECT capture FROM full_records WHERE state='captured' ORDER BY rowid")]
        if not captures:
            raise CrawlError('No files could be recovered in the approved scope for 1999–2006. Sources and catalog progress are retained.')
        notes = list(base_manifest.get('notes', [])) if base_manifest else []
        notes += [{'candidate_id': row['site'], 'url': row['url'], 'timestamp': row['timestamp'], 'note': row['note']}
                 for row in db.execute("SELECT site,url,timestamp,note FROM full_records WHERE state='unavailable' ORDER BY rowid")]
        failed_queries = [dict(row) for row in db.execute('SELECT url,site,note FROM full_queries WHERE note IS NOT NULL')]
        notes += [{'candidate_id': row['site'], 'url': row['url'], 'note': 'Supporting-file lookup failed: ' + row['note']} for row in failed_queries]
        missing = [dict(row) for row in db.execute('SELECT url,site FROM full_queries WHERE found=0 AND note IS NULL')
                   if (row['url'], row['site']) in dependencies]
        notes += [{'candidate_id': row['site'], 'url': row['url'], 'note': 'Referenced supporting file has no successful archived version in 1999–2006.'} for row in missing]
        coverage = {}
        for site in sites:
            gaps = sum(row['site'] == site['id'] for row in db.execute("SELECT site FROM full_records WHERE state='unavailable'"))
            retry = {'files': gaps, 'lookups': sum(row['site'] == site['id'] for row in missing + failed_queries)}
            gaps += retry['lookups']
            if base_manifest:
                board = base_manifest.get('ezboard') or base_manifest.get('sitepowerup') or {}
                counts = board.get('coverage', {}).get('counts', {})
                gaps += counts.get('unavailable', 0) + counts.get('excluded', 0)
            coverage[site['id']] = {'state': 'complete_with_gaps' if gaps else 'complete', 'unavailable': gaps, 'retry': retry,
                'reason': 'The site inventory and listed versions were processed for 1999–2006. ' +
                          (f'{gaps} file versions or supporting-file lookups remain unavailable; see the URLs, known dates and reasons below.' if gaps else 'Wayback may not have archived every original file.')}
        manifest = {**(base_manifest or {}), 'schema': 1, 'batch_id': batch_id, 'created_at': config['created_at'], 'capture_policy': POLICY,
                    'capture_window': dict(CAPTURE_WINDOW), 'sites': sites, 'captures': captures,
                    'capture_coverage': coverage, 'notes': notes, 'limits': config['limits'],
                    'capture_retry': {'files': db.execute("SELECT COUNT(*) FROM full_records WHERE state='unavailable'").fetchone()[0],
                                      'lookups': len(failed_queries) + len(missing)},
                    'transport': store.get('wayback_transport', {}),
                    'indexing': dict((retained or base_manifest or {}).get('indexing', DEFAULT_POLICY)), 'visited': []}
        check_manifest(root, manifest)
        save(result_path, manifest)
        report('ready_for_review')
        return manifest
    except CrawlError as error:
        if 'budget' in str(error).lower():
            store.set('extend_transport_on_resume', True)
        raise
    finally:
        if downloader:
            downloader.close()
        store.close()
