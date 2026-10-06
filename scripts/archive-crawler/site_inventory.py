"""Bounded site/account checks against Git trees, independent of page inventory."""

import re
import subprocess
import time
from urllib.parse import unquote_plus, urlsplit

from common import CrawlError, original_url, site_identity, site_scope, tier, within_scope

VERSION = 1


class SiteInventory:
    def __init__(self, archive, max_trees=4096, max_bytes=32 * 1024 * 1024, max_seconds=15):
        self.archive = archive
        self.hosts = {row['host'].lower(): dict(row) for row in archive.store.db.execute('SELECT * FROM hosts')}
        self.trees, self.process = {}, None
        self.maximum, self.byte_limit, self.seconds = max_trees, max_bytes, max_seconds
        self.bytes, self.started, self.probes = 0, time.monotonic(), 0

    def close(self):
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.process = None

    def tree(self, oid):
        if oid in self.trees:
            return self.trees[oid]
        if (self.probes >= self.maximum or len(self.trees) >= 16384 or self.bytes >= self.byte_limit
                or time.monotonic() - self.started > self.seconds):
            raise CrawlError('Site inventory metadata budget reached')
        if not re.fullmatch(r'[a-f0-9]{40}', oid):
            raise CrawlError('Invalid archive tree identity')
        if self.process is None:
            self.process = subprocess.Popen(['git', '--git-dir', str(self.archive.reader), 'cat-file', '--batch'],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env=self.archive.env)
        self.process.stdin.write((oid + '\n').encode())
        self.process.stdin.flush()
        header = self.process.stdout.readline().split()
        if len(header) != 3 or header[1] != b'tree':
            raise CrawlError('Archive metadata unavailable locally; no fetch attempted')
        size = int(header[2])
        if size > 2 * 1024 * 1024 or self.bytes + size > self.byte_limit:
            raise CrawlError('Site inventory metadata byte budget reached')
        data = self.process.stdout.read(size)
        if len(data) != size or self.process.stdout.read(1) != b'\n':
            raise CrawlError('Archive tree metadata is incomplete')
        self.bytes += size
        self.probes += 1
        entries, offset = {}, 0
        while offset < len(data):
            end = data.index(b'\0', offset)
            mode, name = data[offset:end].split(b' ', 1)
            entries[name.decode('utf-8', 'surrogateescape')] = (mode, data[end+1:end+21].hex())
            offset = end + 21
        self.trees[oid] = entries
        return entries

    def check(self, url, force=False, timestamps=()):
        self.probes = 0
        url = original_url(url)
        identity = site_identity(url)
        cache_key = 'site_inventory:' + repr((VERSION, self.archive.sha, identity))
        cached = self.archive.store.get(cache_key)
        if cached and (not force or cached['status'] != 'inventory_partial'):
            return cached
        parsed = urlsplit(site_scope(url))
        names = (parsed.netloc, parsed.netloc.removeprefix('www.'), 'www.' + parsed.netloc.removeprefix('www.'))
        aliases = dict.fromkeys(alias for name in names for alias in
                                (name, name.replace(':', '_'), name + '_80', name + '_443'))
        matches = [self.hosts[alias] for alias in aliases if alias in self.hosts]
        matches.sort(key=lambda row: row['host'])
        result = {'status': 'new_site', 'archive_sha': self.archive.sha, 'identity_basis': 'site_account',
                  'owner_scope': site_scope(url), 'complete': True}
        if not matches:
            self.archive.store.set(cache_key, result)
            return result
        ordinary = (parsed.path == '/' and not parsed.query
                    and within_scope(parsed.scheme + '://' + parsed.netloc + '/probe/', site_scope(url)))
        if ordinary:
            result.update(status='already_archived', archive_host=matches[0]['host'], archive_path='websites/' + matches[0]['host'])
            self.archive.store.set(cache_key, result)
            return result
        path = unquote_plus(parsed.path.lstrip('/') + ('?' + parsed.query if parsed.query else ''))
        paths = [path.rstrip('/')]
        # Legacy extensionless URLs and explicit .html board links can denote
        # the same EZboard account. Do not merge unrelated boards or forums.
        if parsed.hostname.endswith('.ezboard.com') and re.fullmatch(r'b[^/]+/?', parsed.path.lstrip('/')):
            base = re.sub(r'\.html?$', '', path.rstrip('/'), flags=re.I)
            paths = [base, base + '.html', base + '.htm']
        progress = self.archive.store.get(cache_key + ':cursor') or {}
        cursor = progress.get('position')
        timestamps = progress.get('timestamps', list(timestamps))
        passed, current_cursor = not cursor, None
        try:
            for host in matches:
                captures = self.tree(host['tree']).items()
                captures = sorted(captures, key=lambda entry: (entry[0] not in timestamps, tier(entry[0]) or 3, entry[0]))
                for timestamp, (mode, oid) in captures:
                    if mode != b'40000' or not re.fullmatch(r'\d{14}', timestamp):
                        continue
                    current_cursor = [host['host'], timestamp]
                    if not passed:
                        if current_cursor != cursor:
                            continue
                        passed = True
                    for path in paths:
                        current = oid
                        pieces = path.split('/') if path else ['index.html']
                        for index, piece in enumerate(pieces):
                            entry = self.tree(current).get(piece)
                            if entry is None:
                                break
                            if index == len(pieces) - 1:
                                result.update(status='already_archived', archive_host=host['host'],
                                              archive_path=f"websites/{host['host']}/{timestamp}/{path or 'index.html'}")
                                self.archive.store.set(cache_key, result)
                                return result
                            if entry[0] != b'40000':
                                break
                            current = entry[1]
        except (CrawlError, OSError, ValueError):
            result.update(status='inventory_partial', complete=False)
            if current_cursor:
                self.archive.store.set(cache_key + ':cursor', {'position': current_cursor, 'timestamps': timestamps})
        finally:
            self.close()
        self.archive.store.set(cache_key, result)
        return result
