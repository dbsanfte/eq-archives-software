"""Fill Candidates incrementally under one durable time and Luna budget."""
import json
import os
import time

from acquisition import Downloader, sample
from common import CrawlError, capture_scope, now, site_identity
from coverage_check import require_new
from crawler import parser
from discovery import discover
from grading import Luna, grade
from graph import staged_links
from state import connect
from review import checked_sources, record

SECONDS = 3600


def merge_site(root, operation, row, minimum):
    """Insert once, with an atomic receipt; never replace a human decision."""
    with connect(root) as main:
        main.db.execute('BEGIN IMMEDIATE')
        receipt = main.db.execute("SELECT detail FROM events WHERE candidate=? AND action='discovery_result' AND json_extract(detail,'$.operation')=?",
                                  (row['id'], operation['id'])).fetchone()
        if receipt:
            return json.loads(receipt[0])['accepted']
        if any(site_identity(item['url']) == site_identity(row['url']) for item in main.candidates()):
            return False
        record = dict(row)
        record['scope'] = capture_scope(record['url'])
        record['captures'] = json.dumps([{**capture, 'path': f"runs/{operation['id']}/" + capture['path']}
                                        for capture in json.loads(record['captures'])])
        keys = list(record)
        main.db.execute(f"INSERT INTO candidates({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", list(record.values()))
        rating = json.loads(record['rating'] or 'null')
        accepted = rating is not None and rating['grade'] >= minimum
        main.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                        (row['id'], 'discovery_result', json.dumps({'operation': operation['id'], 'accepted': accepted}), now()))
        main.db.commit()
        return accepted


def owned_or_new(root, operation, row):
    with connect(root) as main:
        owned = main.db.execute("SELECT 1 FROM events WHERE candidate=? AND action='discovery_result' AND json_extract(detail,'$.operation')=?",
                                (row['id'], operation['id'])).fetchone()
        exists = any(site_identity(item['url']) == site_identity(row['url']) for item in main.candidates())
        if owned:
            return 'owned'
        if exists:
            return 'duplicate'
        require_new(main, row['url'])
        return 'new'


def retain_graph(root, store):
    # Metadata only, once per invocation. Reuse local seed/staging scans in later
    # runs without copying source trees or replacing a newer archive snapshot.
    with connect(root) as main:
        if not store.get('archive_sha') or store.get('archive_sha') != main.get('archive_sha'):
            return
        main.db.execute('ATTACH DATABASE ? AS discovery', (str(store.root / 'crawl.sqlite3'),))
        for table in ('hosts', 'tree_state', 'files', 'scans', 'links'):
            main.db.execute(f'INSERT OR IGNORE INTO {table} SELECT * FROM discovery.{table}')
        main.db.commit()


def fill(root, operation, store):
    payload = operation['payload']
    # Deadline starts when the worker claims the run, not while it waits in line.
    # Explicit resumes retain this deadline and every spend/transport reservation.
    checkpoint = store.get('fill_checkpoint') or {'started_at': now(), 'deadline': time.time() + SECONDS,
                                                  'done': [], 'accepted': [], 'skipped': [], 'seed_scanned': False,
                                                  'graph_reads': 0, 'graph_bytes': 0}
    store.set('fill_checkpoint', checkpoint)
    minimum = payload['min_grade']
    options = parser()
    base = ['--work-dir', str(store.root)]
    sample_args = options.parse_args(base + ['sample', '--max-candidates', '1', '--max-requests', '1200',
                                             '--max-bytes', '134217728', '--max-seconds', '3600'])
    grade_args = options.parse_args(base + ['grade', '--api-key-file', '/run/secrets/luna_api_key',
                                            '--max-candidates', '1', '--max-usd', str(payload['max_usd'])])
    downloader = client = None
    snapshot = {}

    def progress(phase, reason=None):
        nonlocal snapshot
        spending = store.db.execute('SELECT COALESCE(SUM(actual),0),COALESCE(SUM(reserved),0) FROM attempts').fetchone()
        snapshot = {'phase': phase, 'stop_reason': reason, 'target': payload['max_candidates'],
                    'min_grade': minimum, 'accepted': len(checkpoint['accepted']), 'checked': len(checkpoint['done']),
                    'skipped': len(checkpoint['skipped']), 'started_at': checkpoint['started_at'],
                    'deadline': checkpoint['deadline'], 'remaining_seconds': max(0, checkpoint['deadline'] - time.time()),
                    'estimated_usd': spending[0], 'reserved_usd': spending[1], 'max_usd': payload['max_usd']}
        store.set('fill_checkpoint', checkpoint)
        with connect(root) as main:
            main.db.execute('UPDATE operations SET result=?,updated=? WHERE id=?',
                            (json.dumps({'progress': snapshot}), now(), operation['id']))
            main.db.commit()
        return {'progress': snapshot, 'candidates': len(checkpoint['done']) - len(checkpoint['skipped'])}

    def finish(reason):
        checkpoint['stop_reason'] = reason
        return progress('complete', reason)

    try:
        if checkpoint.get('stop_reason'):
            return finish(checkpoint['stop_reason'])
        store.set('acquisition_started', store.get('acquisition_started') or now())
        while True:
            if len(checkpoint['accepted']) >= payload['max_candidates']:
                return finish('target_reached')
            if time.time() >= checkpoint['deadline']:
                return finish('time_limit')
            reserved = store.db.execute('SELECT COALESCE(SUM(reserved),0) FROM attempts').fetchone()[0]
            if reserved >= payload['max_usd']:
                return finish('spend_limit')
            pending = [row for row in store.candidates() if row['id'] not in checkpoint['done']]
            if not pending:
                progress('finding_links')
                graph = staged_links(root, store, max_reads=max(0, 66 - checkpoint['graph_reads']),
                                     max_bytes=max(0, 16 * 1024 * 1024 - checkpoint['graph_bytes']))
                checkpoint['graph_reads'] += graph['staged_source_reads']
                checkpoint['graph_bytes'] += graph['staged_source_bytes']
                store.set('fill_checkpoint', checkpoint)
                args = options.parse_args(base + ['discover', '--archive-repo', os.environ.get('ARCHIVE_REPO', '/archive'),
                                                  '--max-candidates', str(len(store.candidates()) + 50)])
                discover(args, store, cached_only=checkpoint['seed_scanned'], deadline=checkpoint['deadline'])
                checkpoint['seed_scanned'] = True
                progress('finding_links')
                pending = [row for row in store.candidates() if row['id'] not in checkpoint['done']]
                if not pending:
                    return finish('time_limit' if time.time() >= checkpoint['deadline'] else 'links_exhausted')
            row = pending[0]
            progress('checking_coverage')
            try:
                ownership = owned_or_new(root, operation, row)
            except CrawlError:
                # Unknown coverage must never reach a paid call. Discovery can
                # continue with other independently verified new websites.
                ownership = 'duplicate'
            if ownership == 'duplicate':
                checkpoint['done'].append(row['id'])
                checkpoint['skipped'].append(row['id'])
                continue
            transport_error = None
            if ownership != 'owned':
                if time.time() >= checkpoint['deadline']:
                    return finish('time_limit')
                if not json.loads(row['captures']):
                    progress('sampling')
                    if downloader is None:
                        previous = store.get('wayback_transport', {}) or {}
                        sample_args.max_seconds = previous.get('seconds', 0) + max(0, checkpoint['deadline'] - time.time())
                        downloader = Downloader(store, sample_args)
                    sample(sample_args, store, candidates=[row], downloader=downloader)
                    row = store.db.execute('SELECT * FROM candidates WHERE id=?', (row['id'],)).fetchone()
                    transport_error = row['error']
                if time.time() < checkpoint['deadline'] and json.loads(row['captures']) and not row['rating']:
                    progress('grading')
                    require_new_before_grade = owned_or_new(root, operation, row)
                    if require_new_before_grade != 'new':
                        checkpoint['done'].append(row['id'])
                        checkpoint['skipped'].append(row['id'])
                        continue
                    attempted = store.db.execute('SELECT 1 FROM attempts WHERE candidate=?', (row['id'],)).fetchone()
                    if attempted:
                        # A lost response is paid/uncertain, not a free retry.
                        store.db.execute("UPDATE candidates SET state='grade_error',error=? WHERE id=?",
                                         ('Previous grading attempt did not produce a saved grade. Use Get evidence & grade to retry explicitly.', row['id']))
                        store.db.commit()
                    else:
                        if client is None:
                            client = Luna(grade_args.api_key_file, deadline=checkpoint['deadline'])
                        grade(grade_args, store, candidates=[row], client=client)
                    row = store.db.execute('SELECT * FROM candidates WHERE id=?', (row['id'],)).fetchone()
            if ownership != 'owned' and row['rating']:
                try:
                    checked_sources(store, record(store, row))
                except CrawlError as error:
                    store.db.execute("UPDATE candidates SET state='grade_error',rating=NULL,error=? WHERE id=?", (str(error), row['id']))
                    store.db.commit()
                    row = store.db.execute('SELECT * FROM candidates WHERE id=?', (row['id'],)).fetchone()
            accepted = merge_site(root, operation, row, minimum)
            checkpoint['done'].append(row['id'])
            if accepted:
                checkpoint['accepted'].append(row['id'])
            progress('checking_coverage')
            error = row['error'] or transport_error or ''
            if time.time() >= checkpoint['deadline']:
                return finish('time_limit')
            if 'Luna dollar budget' in error:
                return finish('spend_limit')
            if any(part in error for part in ('budget', '422 after', '429 after', 'HTTP 401', 'HTTP 403')):
                raise CrawlError(error)
    except CrawlError:
        if time.time() >= checkpoint['deadline']:
            return finish('time_limit')
        progress('paused')
        raise
    finally:
        if downloader is not None:
            downloader.close()
        if client is not None:
            client.close()
        retain_graph(root, store)
