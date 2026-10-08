"""Resolve deep Ezboard links before creating or grading board candidates."""
from pathlib import Path

from common import CrawlError, TIERS, digest, now, original_url, tier
from ezboard import address, candidate_url, remember_page, source_page


def resolve(store, url, downloader):
    parent = candidate_url(store, url)
    if parent:
        return parent
    item = address(url)
    if not item:
        raise CrawlError('This is not an Ezboard board, forum or message URL')
    key = 'ezboard_resolution:' + digest(url)
    saved = store.get(key) or {'url': url, 'captures': [], 'tiers_done': []}
    for capture in saved['captures']:
        source = store.root / capture['path']
        if not source.is_file() or source.is_symlink() or store.root not in source.resolve().parents:
            raise CrawlError('Saved Ezboard identity evidence is unavailable')
        if source.stat().st_size != capture['bytes'] or capture['bytes'] > 1048576:
            raise CrawlError('Saved Ezboard identity evidence changed')
        data = source.read_bytes()
        if digest(data) != capture['sha256']:
            raise CrawlError('Saved Ezboard identity evidence changed')
        page, _ = source_page(data, capture['url'], capture.get('content_type') or '')
        parent = remember_page(store, page, capture)
        if parent:
            store.set(key, {**saved, 'board_url': parent, 'source_path': capture['path']})
            return parent
    for level, (start, end) in TIERS.items():
        if level in saved['tiers_done']:
            continue
        result = downloader.call({'op': 'list', 'url': url, 'from': start, 'to': end})
        records = sorted(result['captures'], key=lambda record: record['timestamp'])
        selected = [records[0]] + ([records[-1]] if len(records) > 1 else []) if records else []
        for record in selected:
            if any(c['requested_timestamp'] == record['timestamp'] for c in saved['captures']):
                continue
            if original_url(record['url']) != original_url(url) or tier(record['timestamp']) != level:
                raise CrawlError('Ezboard identity lookup returned a different page/date')
            path = 'ezboard-evidence/' + digest([url, record['timestamp']]) + '.html'
            destination = store.root / path
            destination.parent.mkdir(exist_ok=True, mode=0o700)
            try:
                capture = downloader.call({'op': 'capture', 'url': record['url'], 'timestamp': record['timestamp'],
                                           'from': start, 'to': end, 'destination': str(destination)})
            except CrawlError as error:
                if str(error) in ('Wayback HTTP 404', 'Wayback HTTP 410'):
                    continue
                raise
            data = destination.read_bytes()
            if original_url(capture['url']) != original_url(url) or tier(capture['timestamp']) != level or digest(data) != capture['sha256']:
                raise CrawlError('Ezboard parent evidence failed identity/hash checks')
            capture.update(path=path, tier=level, retrieved_at=now(), source='wayback')
            saved['captures'].append(capture)
            store.set(key, saved)
            page, encoding = source_page(data, capture['url'], capture.get('content_type') or '')
            capture.update(encoding=encoding, title=' '.join(page.title), site_coverage='ezboard_parent_evidence')
            parent = remember_page(store, page, capture)
            if parent:
                store.set(key, {**saved, 'board_url': parent, 'source_path': capture['path']})
                return parent
        saved['tiers_done'].append(level)
        store.set(key, saved)
    store.set(key, {**saved, 'unresolved': True})
    return None


def evidence(store, linked_url, parent):
    """Reuse only the exact sources that resolved this parent in this run."""
    saved = store.get('ezboard_resolution:' + digest(linked_url)) or {}
    if saved.get('board_url') == parent:
        return [capture for capture in saved.get('captures', []) if capture['path'] == saved.get('source_path')]
    return []
