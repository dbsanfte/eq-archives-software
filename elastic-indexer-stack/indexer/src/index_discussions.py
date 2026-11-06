"""Bounded, resumable source-proof metadata backfill. Never reembeds documents.

Use an explicit host scope and an existing prepared Git checkout. Two passes
learn Lithium RSS memberships, then annotate independently verified pages.
Only the discussion object is changed, with optimistic concurrency checks.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time

import httpx

from indexer.archive_images import Inventory
from indexer.discussions import Catalog, MAX_BYTES, VERSION, lithium_feed, metadata, original_url

FIELDS = ['id', 'url', 'parent_id', 'discussion', 'archive_source_sha256', 'text_passage_provenance.decoding.source_sha256']


class ForumPaths:
    @staticmethod
    def search(name):
        path = name.split('/', 1)[-1]
        return bool(re.search(r'(?:^|/)(?:rss/message|[^/]+/board/(?:message|print)|viewtopic\.php|showthread\.php|topic\.asp|pop_printer_friendly\.asp|[^/]+\.showMessage(?:Range)?)\?', path))


class BackfillInventory(Inventory):
    pattern = ForumPaths

    def __init__(self, repo, state):
        super().__init__(repo, state)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS discussion_cursors (
            host TEXT NOT NULL, phase TEXT NOT NULL, ordinal INTEGER NOT NULL, complete INTEGER NOT NULL,
            PRIMARY KEY(host,phase));
            CREATE TABLE IF NOT EXISTS discussion_gaps (
            id TEXT NOT NULL, phase TEXT NOT NULL, blob TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(id,phase));''')
        old = self.db.execute("SELECT value FROM metadata WHERE key='discussion_policy'").fetchone()
        if old and old[0] != VERSION:
            raise ValueError('Discussion policy changed; use a new private backfill inventory')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('discussion_policy',?)", (VERSION,))
        self.db.commit()

    def scope(self, hosts):
        if not hosts or any(not re.fullmatch(r'[A-Za-z0-9_.:-]{1,253}', host) for host in hosts):
            raise ValueError('Use explicit literal archive hosts')
        hosts = list(dict.fromkeys(hosts))
        encoded = json.dumps(sorted(hosts))
        previous = self.db.execute("SELECT value FROM metadata WHERE key='discussion_hosts'").fetchone()
        if previous and previous[0] != encoded:
            raise ValueError('Backfill host scope changed; retain its original scope')
        known = {row[0]: row[1] for row in self.db.execute('SELECT name,tree FROM hosts')}
        if not set(hosts) <= set(known):
            raise ValueError('Host absent from the pinned source snapshot')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('discussion_hosts',?)", (encoded,))
        self.db.commit()
        return [(host, known[host]) for host in hosts]

    def cursor(self, host, phase):
        return self.db.execute('SELECT ordinal,complete FROM discussion_cursors WHERE host=? AND phase=?', (host, phase)).fetchone() or (0, 0)

    def checkpoint_phase(self, host, phase, ordinal, complete):
        self.db.execute('INSERT OR REPLACE INTO discussion_cursors VALUES (?,?,?,?)', (host, phase, ordinal, int(complete)))
        self.db.commit()

    def gap_phase(self, key, phase, blob, reason):
        self.db.execute('INSERT OR REPLACE INTO discussion_gaps VALUES (?,?,?,?)', (key, phase, blob, reason))
        self.db.commit()


def read_source(inventory, key, blob):
    raw_path = inventory.repo / key
    if any(parent.is_symlink() for parent in (raw_path, *raw_path.parents)
           if parent != inventory.repo and inventory.repo in parent.parents):
        raise ValueError('Source path contains a symbolic link')
    path = raw_path.resolve()
    if inventory.repo not in path.parents:
        raise ValueError('Source path escapes the prepared checkout')
    if path.is_file():
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('Discussion source exceeds the parser byte bound')
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
    else:
        try:
            size = int(inventory.git('cat-file', '-s', blob))
            if size > MAX_BYTES:
                raise ValueError('Discussion source exceeds the parser byte bound')
            raw = inventory.git('cat-file', 'blob', blob)
        except ValueError:
            raise FileNotFoundError('Source is unavailable locally; no fetch attempted') from None
    if len(raw) > MAX_BYTES or hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != blob:
        raise ValueError('Source differs from its pinned Git blob')
    return raw


class Elasticsearch:
    def __init__(self, endpoint, index, password_file, username='elastic'):
        self.endpoint = endpoint.rstrip('/') + '/' + index
        self.client = httpx.Client(auth=(username, Path(password_file).read_text().strip()),
                                   timeout=60, trust_env=False)

    def request(self, method, path, body):
        try:
            response = self.client.request(method, self.endpoint + path, json=body)
        except httpx.HTTPError:
            raise RuntimeError('Metadata transport unavailable; source checkpoints retained') from None
        if response.status_code == 409:
            return None
        if response.status_code != 200:
            raise RuntimeError('Metadata service HTTP ' + str(response.status_code) + '; upstream details omitted')
        return response.json()

    def records(self, keys):
        result = self.request('POST', '/_mget?_source_includes=' + ','.join(FIELDS), {'ids': keys})
        rows = result.get('docs', [])
        if len(rows) != len(keys) or {row.get('_id') for row in rows} != set(keys) or any(row.get('error') or type(row.get('found')) is not bool for row in rows):
            raise ValueError('Incomplete metadata lookup')
        return {row['_id']: row for row in rows}

    def update(self, record, discussion):
        from urllib.parse import quote
        response = self.request('POST', '/_update/' + quote(record['_id'], safe=''), {
            'doc': {'discussion': discussion}, 'if_seq_no': record['_seq_no'], 'if_primary_term': record['_primary_term']})
        if response is None:
            return False
        if response.get('result') not in ('updated', 'noop'):
            raise ValueError('Metadata update was not acknowledged')
        return True

    def close(self):
        self.client.close()


def annotate(record, raw, catalog):
    if not record.get('found'):
        return None
    source = record.get('_source', {})
    if source.get('id') != record['_id'] or source.get('parent_id') or not original_url(record['_id'], source.get('url')):
        raise ValueError('Stored source identity differs from its capture')
    fingerprint = hashlib.sha256(raw).hexdigest()
    saved = source.get('archive_source_sha256') or (source.get('text_passage_provenance') or {}).get('decoding', {}).get('source_sha256')
    if saved and saved != fingerprint:
        raise ValueError('Stored source hash differs from prepared capture')
    return metadata(record['_id'], raw, source.get('url'), catalog=catalog, strict=True)


def backfill(inventory, catalog, client, hosts, *, max_files=10000, max_bytes=256*1024*1024,
             max_seconds=600, clock=time.monotonic, learn_only=False):
    if max_files < 1 or max_bytes < MAX_BYTES or not 1 <= max_seconds <= 3600:
        raise ValueError('Invalid discussion backfill bounds')
    scope = inventory.scope(hosts)
    deadline = clock() + max_seconds
    stats = {'read': 0, 'bytes': 0, 'learned': 0, 'updated': 0, 'unchanged': 0, 'unverified': 0, 'not_indexed': 0, 'conflicts': 0, 'failed': 0}
    for phase in (('learn',) if learn_only else ('learn', 'annotate')):
        for host, tree in scope:
            after, complete = inventory.cursor(host, phase)
            if complete:
                continue
            for batch, ordinal, finished in inventory.batches(host, tree, after):
                if stats['read'] >= max_files or clock() >= deadline:
                    return {**stats, 'complete': False, 'pause_reason': 'operation_budget'}
                candidates = [row for row in batch if phase != 'learn' or '/rss/message?' in row[1]]
                records = client.records([row[1] for row in candidates]) if phase == 'annotate' and candidates else {}
                for position, key, blob in candidates:
                    if phase == 'annotate' and not records[key].get('found'):
                        stats['not_indexed'] += 1
                        inventory.checkpoint_phase(host, phase, position, False)
                        continue
                    if stats['read'] >= max_files or stats['bytes'] + MAX_BYTES > max_bytes or clock() >= deadline:
                        return {**stats, 'complete': False, 'pause_reason': 'operation_budget'}
                    acknowledged = True
                    try:
                        raw = read_source(inventory, key, blob)
                        stats['read'] += 1; stats['bytes'] += len(raw)
                        if phase == 'learn':
                            evidence = lithium_feed(key, raw)
                            catalog.learn(evidence)
                            stats['learned'] += bool(evidence)
                        else:
                            record = records[key]
                            discussion = annotate(record, raw, catalog)
                            if not record.get('found'):
                                stats['not_indexed'] += 1
                            elif not discussion:
                                stats['unverified'] += 1
                            elif record['_source'].get('discussion') == discussion:
                                stats['unchanged'] += 1
                            elif client.update(record, discussion):
                                stats['updated'] += 1
                            else:
                                stats['conflicts'] += 1
                                acknowledged = False
                                inventory.gap_phase(key, phase, blob, 'concurrent_update')
                    except (ValueError, FileNotFoundError, RecursionError) as error:
                        inventory.gap_phase(key, phase, blob, type(error).__name__)
                        stats['failed'] += 1
                    else:
                        if acknowledged:
                            inventory.db.execute('DELETE FROM discussion_gaps WHERE id=? AND phase=?', (key, phase))
                    inventory.checkpoint_phase(host, phase, position, False)
                inventory.checkpoint_phase(host, phase, ordinal, finished)
                print(json.dumps({'host': host, 'phase': phase, 'ordinal': ordinal, 'progress': stats}), flush=True)
    if learn_only:
        return {**stats, 'complete': False, 'prepared': True, 'pause_reason': 'evidence_prepared'}
    gaps = inventory.db.execute('SELECT COUNT(*) FROM discussion_gaps').fetchone()[0]
    return {**stats, 'complete': True, 'pending_gaps': gaps}



def retry_gaps(inventory, catalog, client, hosts, *, limit=1000):
    if not 1 <= limit <= 1000:
        raise ValueError('Retry 1–1000 discussion metadata gaps at a time')
    inventory.scope(hosts)
    rows=inventory.db.execute("SELECT id,phase,blob FROM discussion_gaps ORDER BY CASE phase WHEN 'learn' THEN 0 ELSE 1 END,id LIMIT ?",(limit,)).fetchall()
    result={'attempted':0,'resolved':0,'pending':0}
    for key,phase,blob in rows:
        if key.split('/')[1] not in hosts:
            raise ValueError('Gap is outside its saved host scope')
        result['attempted']+=1
        try:
            raw=read_source(inventory,key,blob)
            if phase=='learn':
                catalog.learn(lithium_feed(key,raw))
                # Recovered evidence can verify previously unknown print views.
                inventory.db.execute("DELETE FROM discussion_cursors WHERE host=? AND phase='annotate'",(key.split('/')[1],))
            else:
                record=client.records([key])[key]
                value=annotate(record,raw,catalog)
                if not value or (record['_source'].get('discussion')!=value and not client.update(record,value)):
                    continue
        except (ValueError,FileNotFoundError):
            continue
        inventory.db.execute('DELETE FROM discussion_gaps WHERE id=? AND phase=?',(key,phase));inventory.db.commit()
        result['resolved']+=1
    result['pending']=inventory.db.execute('SELECT COUNT(*) FROM discussion_gaps').fetchone()[0]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--host', action='append', required=True)
    parser.add_argument('--es-url', default=os.environ.get('ELASTICSEARCH_URL', 'http://elasticsearch.eqarchives-es.svc.cluster.local:9200'))
    parser.add_argument('--index', default='eq-archive')
    parser.add_argument('--password-file', type=Path, default=Path('/run/secrets/es_password'))
    parser.add_argument('--learn-only', action='store_true')
    parser.add_argument('--retry-gaps', action='store_true')
    parser.add_argument('--max-files', type=int, default=10000)
    parser.add_argument('--max-bytes', type=int, default=256*1024*1024)
    parser.add_argument('--max-seconds', type=int, default=600)
    args = parser.parse_args()
    inventory = BackfillInventory(args.repo, args.state)
    catalog = Catalog(args.catalog)
    client = None if args.learn_only else Elasticsearch(args.es_url, args.index, args.password_file)
    try:
        if args.retry_gaps:
            result=retry_gaps(inventory,catalog,client,args.host)
            print(json.dumps({'retry':result}),flush=True)
            return 1 if result['pending'] else 0
        result = backfill(inventory, catalog, client, args.host, max_files=args.max_files, max_bytes=args.max_bytes, max_seconds=args.max_seconds,learn_only=args.learn_only)
        print(json.dumps({'result': result}), flush=True)
        return 0 if result.get('prepared') else 2 if not result['complete'] else 1 if result.get('pending_gaps') else 0
    finally:
        if client:client.close()
        catalog.close(); inventory.close()


if __name__ == '__main__':
    raise SystemExit(main())
