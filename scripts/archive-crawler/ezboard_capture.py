#!/usr/bin/env python3
"""Plan and stage one Ezboard, including forums, message ranges and server moves."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from acquisition import Downloader
from archive_layout import archive_path
from common import LEGACY_TIERS, CrawlError, Store, TIERS, digest, in_capture_window, now, original_url, save, tier
from discovery import Archive
import ezboard

DEFAULT_LIMITS = {'max_captures': 2000, 'max_catalog_rows': 100000, 'max_hosts': 512,
                  'max_requests': 4000, 'max_bytes': 256 * 1024 * 1024, 'max_seconds': 3600,
                  'max_page_bytes': 1024 * 1024, 'delay': 0, 'bytes_per_second': 0}


class BoundReached(CrawlError):
    pass


class Capture:
    def __init__(self, store, platform=ezboard):
        self.platform, self.name = platform, platform.__name__
        if store.get(('sitepowerup' if self.name == 'ezboard' else 'ezboard') + '_config'):
            raise CrawlError('This directory already contains a different board platform. Use its saved capture command.')
        self.store, self.db, self.root = store, store.db, store.root
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS ez_queries (
                host TEXT, kind TEXT, tier INTEGER, resume TEXT, done INTEGER DEFAULT 0,
                PRIMARY KEY(host,kind,tier));
            CREATE TABLE IF NOT EXISTS ez_records (
                id TEXT PRIMARY KEY, url TEXT, stamp TEXT, tier INTEGER, kind TEXT,
                cdx_digest TEXT, cdx_length TEXT, state TEXT DEFAULT 'pending',
                metadata TEXT, reason TEXT);
            CREATE INDEX IF NOT EXISTS ez_pending ON ez_records(state,tier,kind,stamp);
            CREATE INDEX IF NOT EXISTS ez_destination ON ez_records(json_extract(metadata,'$.archive_path')) WHERE state='captured';
            CREATE TABLE IF NOT EXISTS ez_forums (token TEXT PRIMARY KEY, evidence TEXT);
            CREATE TABLE IF NOT EXISTS ez_hosts (host TEXT PRIMARY KEY, evidence TEXT);
        ''')
        self.config = store.get(self.name + '_config')

    def date_tiers(self):
        # A saved operator plan keeps its original date policy on resume.
        return {int(key): value for key, value in self.config.get('date_tiers', LEGACY_TIERS).items()}

    def capture_window(self):
        tiers = self.date_tiers()
        return {'from': tiers[1][0], 'to': tiers[2][1], 'versions': 'all_available'}

    def plan(self, url, hosts=(), limits=None, archive_repo=None):
        if self.config:
            raise CrawlError('This directory already has a plan. Resume capture or use a new private directory.')
        board = self.platform.board_name(url)
        self.config = {'schema': 1, 'board': board, 'url': original_url(url), 'created_at': now(),
                       'limits': {**DEFAULT_LIMITS, **(limits or {})}, 'date_tiers': TIERS}
        self.validate_limits(self.config['limits'])
        names = {self.platform.address(url)['host']: {'source': 'submitted_url', 'url': original_url(url)}}
        for host in hosts:
            if not self.platform.shard(host):
                raise CrawlError('Additional host is not supported by this board platform')
            names[host.lower()] = {'source': 'operator'}
        if archive_repo:
            archive = Archive(archive_repo, self.store)
            self.config['archive_sha'] = archive.sha
            # Root host trees only. Do not enumerate dates/files or fetch blobs.
            for row in self.db.execute('SELECT host FROM hosts'):
                if self.platform.shard(row['host']):
                    names.setdefault(row['host'].lower(), {'source': 'archive_host_inventory', 'sha': archive.sha})
        if len(names) > self.config['limits']['max_hosts']:
            raise CrawlError('Host inventory exceeds the saved host limit')
        self.config['initial_hosts'] = names
        self.store.set(self.name + '_config', self.config)
        self.store.set(self.name + '_catalog_pages', 0)
        for host, evidence in names.items():
            self.add_host(host, evidence)
        self.set_status('planned', 'Plan saved; capture has not started')
        return self.status()

    @staticmethod
    def validate_limits(limits):
        for key in DEFAULT_LIMITS:
            value = limits[key]
            minimum = 0 if key in {'delay', 'bytes_per_second'} else 1
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise CrawlError('Capture limits must be positive integers; delay and transfer rate may be zero to disable pacing')
        if limits['max_page_bytes'] > 32 * 1024 * 1024 or limits['max_hosts'] > 512:
            raise CrawlError('Page/host limits exceed the supported bounds')

    def extend(self, changes):
        self.require_plan()
        limits = {**self.config['limits'], **changes}
        self.validate_limits(limits)
        if any(limits[key] < self.config['limits'][key] for key in changes):
            raise CrawlError('Extensions must increase cumulative limits, without resetting usage')
        before = dict(self.config['limits'])
        self.config['limits'] = limits
        self.store.event(None, self.name + '_limits_extended', {'previous': before, 'limits': limits})
        self.store.set(self.name + '_config', self.config)
        return self.status()

    def require_plan(self):
        if not self.config:
            raise CrawlError('Create a board capture plan first')

    def add_host(self, host, evidence):
        existing = self.db.execute('SELECT evidence FROM ez_hosts WHERE host=?', (host,)).fetchone()
        if existing:
            previous = json.loads(existing['evidence'])
            if previous.get('source') == 'archive_host_inventory' and evidence.get('source') != 'archive_host_inventory':
                # A source-proven move outranks a speculative root inventory host.
                # Retain its original provenance and every catalog checkpoint.
                self.db.execute('UPDATE ez_hosts SET evidence=? WHERE host=?',
                                (json.dumps({**evidence, 'host_inventory': previous}), host))
                self.db.commit()
            return
        if not self.platform.shard(host):
            raise CrawlError('Invalid board host')
        if self.db.execute('SELECT COUNT(*) FROM ez_hosts').fetchone()[0] >= self.config['limits']['max_hosts']:
            raise BoundReached('Board host limit reached; continuation is saved')
        self.db.execute('INSERT INTO ez_hosts VALUES (?,?)', (host, json.dumps(evidence)))
        for level in TIERS:
            for kind in self.platform.CATALOG_KINDS:
                self.db.execute('INSERT INTO ez_queries(host,kind,tier) VALUES (?,?,?)', (host, kind, level))
        self.db.commit()

    def forums(self):
        return {row[0] for row in self.db.execute('SELECT token FROM ez_forums')}

    def learn(self, page, result):
        board = self.config['board']
        parsed = self.platform.candidate(result['url'], board)
        evidence = {key: result[key] for key in ('url', 'timestamp', 'sha256')}
        tokens = self.platform.forum_links(page, result['url'], board)
        if parsed['kind'] != 'board':
            tokens.append(parsed['token'])
        for token in tokens:
            self.db.execute('INSERT OR IGNORE INTO ez_forums VALUES (?,?)', (token, json.dumps(evidence)))
        self.db.commit()
        for link in page.links:
            target = self.platform.candidate(link['url'], board)
            if target:
                self.add_host(target['host'], {**evidence, 'source': 'captured_link', 'link': link['url']})

    def set_status(self, state, reason):
        self.store.set(self.name + '_status', {'state': state, 'reason': reason, 'updated_at': now()})

    def seed(self, captures, source_root):
        """Reuse explicitly reviewed sources without another Wayback request."""
        self.require_plan()
        source_root = Path(source_root).resolve()
        for capture in captures:
            parsed = self.platform.candidate(capture['url'], self.config['board'])
            if not parsed or not in_capture_window(capture['timestamp'], self.capture_window()):
                continue
            source = source_root / capture['path']
            if source_root not in source.resolve().parents or source.is_symlink():
                raise CrawlError('Reviewed board source escaped staging')
            if source.stat().st_size != capture['bytes'] or capture['bytes'] > self.config['limits']['max_page_bytes']:
                raise CrawlError('Reviewed board source size changed')
            data = source.read_bytes()
            if digest(data) != capture['sha256']:
                raise CrawlError('Reviewed board source hash changed')
            page, _ = self.platform.source_page(data, capture['url'], capture.get('content_type') or '')
            if not self.platform.belongs(page, capture['url'], self.config['board'], self.forums()):
                continue
            key = digest([capture['url'], capture['timestamp']])
            if self.db.execute('SELECT 1 FROM ez_records WHERE id=?', (key,)).fetchone():
                continue
            path = self.root / 'downloads' / (key + '.html')
            path.parent.mkdir(exist_ok=True, mode=0o700)
            if path.exists():
                self.read_source(path, capture)
            else:
                os.link(source, path)
            self.db.execute("INSERT INTO ez_records(id,url,stamp,tier,kind,cdx_digest,cdx_length,state,metadata) VALUES (?,?,?,?,?,?,?,'downloaded',?)",
                (key, capture['url'], capture['timestamp'], tier(capture['timestamp']), parsed['kind'],
                 capture.get('cdx_digest'), capture.get('cdx_length'), json.dumps(capture)))
            self.db.commit()
            self.stage(None, self.db.execute('SELECT * FROM ez_records WHERE id=?', (key,)).fetchone())

    def status(self):
        self.require_plan()
        counts = dict(self.db.execute('SELECT state,COUNT(*) FROM ez_records GROUP BY state'))
        catalogs = self.db.execute('SELECT COUNT(*),COALESCE(SUM(done),0) FROM ez_queries').fetchone()
        return {**self.store.get(self.name + '_status', {}), 'board': self.config['board'],
                'capture_window': self.capture_window(),
                'counts': counts, 'hosts': self.db.execute('SELECT COUNT(*) FROM ez_hosts').fetchone()[0],
                'forums': self.db.execute('SELECT COUNT(*) FROM ez_forums').fetchone()[0],
                'catalogs_total': catalogs[0], 'catalogs_completed': catalogs[1],
                'catalogs_remaining': catalogs[0] - catalogs[1],
                'catalog_pages': self.store.get(self.name + '_catalog_pages'),
                'catalog_rows': self.store.get(self.name + '_catalog_rows', 0),
                'limits': self.config['limits'], 'transport': self.store.get('wayback_transport', {})}

    def catalog(self, downloader, query):
        start, end = self.date_tiers()[query['tier']]
        url = self.platform.catalog_url(self.config['board'], query)
        result = downloader.call({'op': self.name + '_list', 'url': url, 'from': start, 'to': end,
                                  'resume_key': query['resume']})
        resume = result['resume_key']
        if resume and resume == query['resume']:
            raise CrawlError('CDX repeated its continuation key; catalog completeness is unresolved')
        records = result['captures']
        used = self.store.get(self.name + '_catalog_rows', 0)
        if used + len(records) > self.config['limits']['max_catalog_rows']:
            raise BoundReached('CDX row budget reached; the current catalog position is retained')
        for record in records:
            parsed = self.platform.candidate(record['url'], self.config['board'])
            if (not parsed or not self.platform.catalog_member(parsed, query)
                    or tier(record['timestamp']) != query['tier'] or not start <= record['timestamp'] <= end):
                continue
            key = digest([record['url'], record['timestamp']])
            self.db.execute('INSERT OR IGNORE INTO ez_records(id,url,stamp,tier,kind,cdx_digest,cdx_length) VALUES (?,?,?,?,?,?,?)',
                            (key, record['url'], record['timestamp'], query['tier'], parsed['kind'], record['digest'], record['length']))
        self.db.execute('UPDATE ez_queries SET resume=?,done=? WHERE host=? AND kind=? AND tier=?',
                        (resume, int(not resume), query['host'], query['kind'], query['tier']))
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (self.name + '_catalog_rows', json.dumps(used + len(records))))
        # Older checkpoints cannot recover an exact historical page count.
        pages = self.store.get(self.name + '_catalog_pages')
        if pages is not None:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (self.name + '_catalog_pages', json.dumps(pages + 1)))
        self.db.commit()

    def stage(self, downloader, row):
        temporary = self.root / 'downloads' / (row['id'] + '.html')
        temporary.parent.mkdir(exist_ok=True, mode=0o700)
        if row['state'] == 'downloaded':
            result = json.loads(row['metadata'])
        else:
            consumed = self.db.execute("SELECT COUNT(*) FROM ez_records WHERE metadata IS NOT NULL").fetchone()[0]
            if consumed >= self.config['limits']['max_captures']:
                raise BoundReached('Capture file limit reached; pending captures are retained')
            start, end = self.date_tiers()[row['tier']]
            try:
                result = downloader.call({'op': 'capture_file' if self.config.get('complete_files') else 'capture', 'url': row['url'], 'timestamp': row['stamp'],
                                          'from': start, 'to': end, 'destination': str(temporary)})
            except CrawlError as error:
                if self.config.get('complete_files') and 'byte limit' in str(error):
                    raise  # an oversized page is not a completed board
                if str(error) not in ('Wayback HTTP 404', 'Wayback HTTP 410', 'Replay returned a different original URL',
                                     'Wayback returned a capture outside the requested tier',
                                     'Replay returned a different dated version; requested version remains unavailable') and 'Response exceeds byte limit' not in str(error):
                    raise
                self.db.execute("UPDATE ez_records SET state='unavailable',reason=? WHERE id=?", (str(error), row['id']))
                self.db.commit()
                return
            # Save the replay's actual URL/date/hash before interpreting HTML or
            # moving it. Resume this receipt without paying for another replay.
            self.db.execute("UPDATE ez_records SET state='downloaded',metadata=? WHERE id=?", (json.dumps(result), row['id']))
            self.db.commit()
        if (result['url'] != row['url'] or tier(result['timestamp']) != row['tier'] or
                not in_capture_window(result['timestamp'], self.capture_window())):
            raise CrawlError('Replay failed exact URL/date validation')
        source_bytes = self.db.execute("SELECT COALESCE(SUM(json_extract(metadata,'$.bytes')),0) FROM ez_records").fetchone()[0]
        if source_bytes > self.config['limits']['max_bytes']:
            raise BoundReached('Uncompressed source byte budget reached; the downloaded receipt is retained')
        destination = archive_path(result)
        output = self.root / 'sources' / destination
        source = temporary if temporary.exists() else output
        data = self.read_source(source, result)
        page, encoding = self.platform.source_page(data, result['url'], result.get('content_type') or '')
        reason = self.platform.source_problem(page)
        if not reason and not self.platform.belongs(page, result['url'], self.config['board'], self.forums()):
            reason = 'Board membership is unverified or points to another board'
        metadata = {**result, 'path': str(source.relative_to(self.root)), 'tier': row['tier'],
                    'cdx_digest': row['cdx_digest'], 'cdx_length': row['cdx_length'], 'retrieved_at': result.get('retrieved_at') or now(),
                    'encoding': encoding, 'title': ' '.join(page.title), 'source': 'wayback',
                    'site_coverage': 'bounded_' + self.name + '_catalog', 'archive_path': destination}
        if reason:
            self.db.execute("UPDATE ez_records SET state='excluded',metadata=?,reason=? WHERE id=?",
                            (json.dumps(metadata), reason, row['id']))
            self.db.commit()
            return
        # No path/protocol/query collision may overwrite an existing source.
        collision = self.db.execute("SELECT metadata FROM ez_records WHERE state='captured' AND json_extract(metadata,'$.archive_path')=? AND id<>?", (destination, row['id']))
        for existing in collision:
            saved = json.loads(existing[0])
            if saved['archive_path'] == destination:
                if saved['url'] == result['url'] and saved['sha256'] == result['sha256']:
                    self.db.execute("UPDATE ez_records SET state='duplicate',metadata=?,reason=? WHERE id=?",
                                    (json.dumps(metadata), 'Replay resolved to an already staged exact capture', row['id']))
                    self.db.commit()
                    return
                raise CrawlError('Different URL identities or content collide in the archive layout; no overwrite attempted')
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if output.resolve() != output or any(parent.is_symlink() for parent in output.parents):
            raise CrawlError('Source destination escaped staging')
        if output.exists():
            # Crash recovery of the same receipt only; reject pre-existing files
            # whose exact identity has not been recorded by this run.
            if result.get('archive_path') != destination or self.read_source(output, result) != data:
                raise CrawlError('Source destination collision')
        else:
            self.db.execute("UPDATE ez_records SET metadata=? WHERE id=?", (json.dumps(metadata), row['id']))
            self.db.commit()
            os.link(temporary, output)  # atomic create-only, never overwrite
        metadata['path'] = str(output.relative_to(self.root))
        # Learn before marking this receipt complete: host-limit interruptions
        # then replay this local receipt and cannot silently lose new aliases.
        self.learn(page, metadata)
        self.db.execute("UPDATE ez_records SET state='captured',metadata=?,reason=NULL WHERE id=?", (json.dumps(metadata), row['id']))
        self.db.commit()
        temporary.unlink(missing_ok=True)

    def read_source(self, path, metadata):
        path = Path(path)
        if self.root not in path.resolve().parents or path.is_symlink():
            raise CrawlError('Source escaped private staging')
        try:
            if path.stat().st_size != metadata['bytes'] or metadata['bytes'] > self.config['limits']['max_page_bytes']:
                raise CrawlError('Source byte count changed')
            data = path.read_bytes()
        except OSError:
            raise CrawlError('Staged source is unavailable') from None
        if digest(data) != metadata['sha256']:
            raise CrawlError('Staged source hash changed')
        return data

    def run(self, downloader_factory=Downloader, progress=None):
        self.require_plan()
        downloader = None
        self.set_status('capturing', 'Reading saved board catalogs and captures')
        try:
            for host, evidence in self.config.get('initial_hosts', {}).items():
                self.add_host(host, evidence)
            while True:
                # Follow submitted/source-proven servers before speculative
                # archive hosts, retaining every host, date and continuation.
                # Board catalogs still precede forums within each priority/tier.
                query = self.db.execute("""SELECT q.* FROM ez_queries q JOIN ez_hosts h ON h.host=q.host
                    WHERE q.done=0 ORDER BY CASE WHEN json_extract(h.evidence,'$.source')='archive_host_inventory'
                    THEN 1 ELSE 0 END,q.tier,q.kind,q.host LIMIT 1""").fetchone()
                row = self.db.execute("SELECT * FROM ez_records WHERE state IN ('pending','downloaded') ORDER BY tier,kind,stamp,url LIMIT 1").fetchone()
                if not query and not row:
                    self.set_status('complete', 'Saved catalogs exhausted for known hosts; Wayback may have uncaptured or unknown-host pages')
                    break
                if not downloader:
                    downloader = downloader_factory(self.store, SimpleNamespace(**self.config['limits']))
                activity = {'phase': 'downloading' if row else 'checking_wayback',
                            'current_url': row['url'] if row else self.platform.catalog_url(self.config['board'], query)}
                if not row:
                    start, end = self.date_tiers()[query['tier']]
                    activity['current_query'] = {'host': query['host'], 'kind': query['kind'], 'from': start, 'to': end}
                if progress:
                    progress({**self.status(), **activity})
                if row:
                    self.stage(downloader, row)
                else:
                    self.catalog(downloader, query)
                if progress:
                    progress({**self.status(), **activity})
        except CrawlError as error:
            bounded = isinstance(error, BoundReached) or 'budget' in str(error).lower()
            self.set_status('bounded' if bounded else 'paused', str(error))
        finally:
            if downloader:
                downloader.close()
        manifest = self.export()
        return manifest

    def export(self):
        self.require_plan()
        captures = [json.loads(row[0]) for row in self.db.execute("SELECT metadata FROM ez_records WHERE state='captured' ORDER BY tier,url,stamp")]
        notes = [dict(row) for row in self.db.execute('SELECT url,stamp,state,reason FROM ez_records WHERE reason IS NOT NULL ORDER BY url,stamp')]
        manifest = {'schema': 1, 'strategy': self.name + '-v1', 'board': self.config['board'],
                    'capture_window': self.capture_window(),
                    'seed_url': self.config['url'], 'created_at': self.config['created_at'],
                    'coverage': self.status(), 'hosts': [dict(row) for row in self.db.execute('SELECT * FROM ez_hosts ORDER BY host')],
                    'forums': [dict(row) for row in self.db.execute('SELECT * FROM ez_forums ORDER BY token')],
                    'captures': captures, 'notes': notes}
        save(self.root / (self.name + '-manifest.json'), manifest)
        return manifest

    def verify(self):
        manifest = self.export()
        for capture in manifest['captures']:
            self.read_source(self.root / capture['path'], capture)
            if capture['archive_path'] != archive_path(capture):
                raise CrawlError('Archive destination changed')
        return {'verified_captures': len(manifest['captures']), 'manifest_sha256': digest(manifest)}


@contextmanager
def locked_store(root):
    root = Path(root).resolve()
    if any((parent / '.git').exists() or (parent / 'HEAD').is_file() and (parent / 'objects').is_dir()
           for parent in (root, *root.parents)):
        raise CrawlError('Keep capture state outside Git repositories')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.ezboard.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CrawlError('Another board capture command is using this directory') from None
        store = Store(root)
        try:
            yield store
        finally:
            store.close()


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--work-dir', required=True, help='Private staging outside both repositories')
    commands = cli.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='Save scope and bounds; no Wayback requests')
    plan.add_argument('--url', required=True, help='Board URL or dated Wayback board link')
    plan.add_argument('--host', action='append', default=[], help='Additional historical shard (repeatable)')
    plan.add_argument('--archive-repo', help='Add numbered Ezboard hosts from the local root Git tree only')
    extend = commands.add_parser('extend', help='Explicitly increase cumulative bounds without resetting usage')
    for key in ('max_captures', 'max_catalog_rows', 'max_requests', 'max_bytes', 'max_seconds'):
        plan.add_argument('--' + key.replace('_', '-'), type=int)
        extend.add_argument('--' + key.replace('_', '-'), type=int)
    commands.add_parser('capture', help='Capture/resume the saved plan; never publish, index or call Luna')
    commands.add_parser('status', help='Read progress metadata without walking sources')
    commands.add_parser('verify', help='Verify staged source hashes and exact archive destinations')
    return cli


def main():
    args = parser().parse_args()
    try:
        with locked_store(args.work_dir) as store:
            capture = Capture(store)
            changes = {key: getattr(args, key) for key in DEFAULT_LIMITS if getattr(args, key, None) is not None}
            if args.command == 'plan':
                result = capture.plan(args.url, args.host, changes, args.archive_repo)
            elif args.command == 'extend':
                result = capture.extend(changes)
            elif args.command == 'capture':
                def progress(status):
                    print(json.dumps(status), flush=True)
                result = capture.run(progress=progress)['coverage']
            elif args.command == 'verify':
                result = capture.verify()
            else:
                result = capture.status()
            print(json.dumps(result, indent=2))
            return 2 if result.get('state') in ('bounded', 'paused') else 0
    except (CrawlError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
