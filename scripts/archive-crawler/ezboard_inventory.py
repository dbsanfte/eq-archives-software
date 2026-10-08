"""One resumable, shared board inventory across Ezboard's numbered servers.

Read Git trees only, through SiteInventory's existing cumulative budgets and
remote-free object reader. Cache account names once per archive revision rather
than rescanning millions of timestamp trees separately for every candidate.
"""
import re
from urllib.parse import urlsplit

from common import CrawlError, tier
from ezboard import address, board_name, shard


def check_board(inventory, url, timestamps=()):
    from site_inventory import InventoryPause
    archive, store = inventory.archive, inventory.archive.store
    hosts = {row['host']: dict(row) for row in store.db.execute('SELECT * FROM hosts') if shard(row['host'])}
    name = board_name(url)
    key = 'ezboard_archive_inventory:v1:' + archive.sha
    state = store.get(key) or {'host': 0, 'date': 0, 'checked': 0, 'preferred_dates': list(timestamps),
        'hosts': sorted(hosts, key=lambda host: (host.lower() != urlsplit(url).netloc, host))}
    result = {'status': 'new_site', 'archive_sha': archive.sha, 'identity_basis': 'ezboard_account',
              'owner_scope': url, 'complete': True}

    def found():
        row = store.db.execute('SELECT host,path FROM ezboard_archive_boards WHERE sha=? AND board=?', (archive.sha, name)).fetchone()
        if row:
            return {**result, 'status': 'already_archived', 'archive_host': row['host'], 'archive_path': row['path']}

    cached = found()
    if cached:
        return cached
    try:
        while state['host'] < len(state['hosts']):
            host = state['hosts'][state['host']]
            dates = sorted([(stamp, entry) for stamp, entry in inventory.tree(hosts[host]['tree']).items()
                            if entry[0] == b'40000' and re.fullmatch(r'\d{14}', stamp)],
                           key=lambda item: (item[0] not in state.get('preferred_dates', []), tier(item[0]) or 3, item[0]))
            while state['date'] < len(dates):
                stamp, (_, tree) = dates[state['date']]
                for path in inventory.tree(tree):
                    item = address('http://' + host + '/' + path)
                    if not item:
                        continue
                    archive_path = f'websites/{host}/{stamp}/{path}'
                    if item['kind'] == 'board':
                        store.db.execute('INSERT OR IGNORE INTO ezboard_archive_boards VALUES (?,?,?,?)',
                                         (archive.sha, item['token'][1:], host, archive_path))
                    else:
                        store.db.execute('INSERT OR IGNORE INTO ezboard_archive_forums VALUES (?,?,?,?)',
                                         (archive.sha, item['token'], host, archive_path))
                state['date'] += 1
                state['checked'] += 1
                cached = found()
                if cached:
                    store.set(key, state)
                    return cached
            state['host'] += 1
            state['date'] = 0
        state['complete'] = True
        # Some old downloads have only forum/message pages. A concatenated
        # prefix is ambiguous, so refuse a clean bill of health in that case.
        possible = store.db.execute('''SELECT f.token,f.host,f.path,a.board AS owner
            FROM ezboard_archive_forums f LEFT JOIN ezboard_aliases a ON a.token=f.token
            WHERE f.sha=? AND substr(f.token,1,?)=? AND (a.board IS NULL OR a.board=?)
            ORDER BY a.board IS NULL,f.token LIMIT 1''', (archive.sha, len(name) + 1, 'f' + name, name)).fetchone()
        if possible:
            if possible['owner'] == name:
                result.update(status='already_archived', archive_host=possible['host'], archive_path=possible['path'])
            else:
                result.update(status='inventory_partial', complete=False, retryable=False, reason='ezboard_owner_unverified',
                              message='Archived forum pages may belong to this board; their parent-board evidence must be verified before approval.')
    except (CrawlError, OSError, ValueError) as error:
        result.update(status='inventory_partial', complete=False,
                      reason=error.reason if isinstance(error, InventoryPause) else 'metadata_unavailable',
                      message=str(error) if isinstance(error, InventoryPause) else 'Ezboard archive metadata is unavailable locally; no fetch attempted.',
                      retryable=error.retryable if isinstance(error, InventoryPause) else False)
    finally:
        inventory.close()
        store.set(key, state)
    result['progress'] = {'host': state['hosts'][state['host']] if state['host'] < len(state['hosts']) else '',
                          'checked': state['host'], 'total': len(state['hosts']), 'snapshots_checked': state['checked']}
    return result
