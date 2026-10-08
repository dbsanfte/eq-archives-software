"""Resumable, shared metadata coverage for query-addressed SitePowerUp boards."""
import re

from common import CrawlError, tier
from sitepowerup import archived_board, board_name, board_url


def check_board(inventory, url, timestamps=()):
    from site_inventory import InventoryPause
    archive, store = inventory.archive, inventory.archive.store
    board = board_name(url)
    key = 'sitepowerup_archive_inventory:v1:' + archive.sha
    hosts = {name: row for name, row in inventory.hosts.items()
             if re.fullmatch(r'(?:www\.)?sitepowerup\.com(?:(?:_|:)(?:80|443))?', name)}
    state = store.get(key) or {'host': 0, 'date': 0, 'checked': 0, 'hosts': sorted(hosts),
                              'preferred_dates': list(timestamps)}
    result = {'status': 'new_site', 'complete': True, 'archive_sha': archive.sha,
              'identity_basis': 'sitepowerup_board_id', 'owner_scope': board_url(url)}

    def found():
        row = store.db.execute('SELECT host,path FROM sitepowerup_archive_boards WHERE sha=? AND board=?', (archive.sha, board)).fetchone()
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
                           key=lambda item: (item[0] not in state['preferred_dates'], tier(item[0]) or 3, item[0]))
            result['progress'] = {'host': host, 'checked': state['date'], 'total': len(dates)}
            while state['date'] < len(dates):
                stamp, (_, root) = dates[state['date']]
                # Only the timestamp's mb directory. No recursive source walks,
                # per-board repeated scans, or page/blob reads.
                for folder, (mode, tree) in inventory.tree(root).items():
                    if folder.lower() != 'mb' or mode != b'40000':
                        continue
                    for name, (mode, _) in inventory.tree(tree).items():
                        identity = archived_board('mb/' + name)
                        if identity and mode in (b'100644', b'100755', b'40000'):
                            store.db.execute('INSERT OR IGNORE INTO sitepowerup_archive_boards VALUES (?,?,?,?)',
                                             (archive.sha, identity, host, f'websites/{host}/{stamp}/{folder}/{name}'))
                state['date'] += 1
                state['checked'] += 1
                result['progress']['checked'] = state['date']
                cached = found()
                if cached:
                    return cached
            state['host'] += 1
            state['date'] = 0
        state['complete'] = True
    except (CrawlError, OSError, ValueError) as error:
        result.update(status='inventory_partial', complete=False,
                      reason=error.reason if isinstance(error, InventoryPause) else 'metadata_unavailable',
                      retryable=error.retryable if isinstance(error, InventoryPause) else False,
                      message=str(error) if isinstance(error, InventoryPause) else 'SitePowerUp archive metadata is unavailable locally; no fetch attempted.')
    finally:
        store.set(key, state)
        inventory.close()
    return result
