"""Optional bounded homepage evidence through the existing Wayback session."""
import json
import re
from urllib.parse import urlsplit

from common import (CrawlError, Page, TIERS, capture_scope, decode, digest,
                    in_capture_window, now, original_url, site_scope, within_capture_scope)
from selection_policy import VERSION, home_source
from wayback_transport import TransportUnavailable


class EvidencePage(Page):
    def __init__(self, url):
        super().__init__(url)
        self.frames = []

    def handle_starttag(self, tag, attrs):
        before = len(self.links)
        super().handle_starttag(tag, attrs)
        if tag in ('frame', 'iframe') and len(self.links) > before:
            self.frames.append(self.links[-1]['url'])


def context(store, candidate, captures, downloader):
    owner = site_scope(candidate['url'])
    has_home = any(home_source(c['url'], owner) for c in captures)
    mode = json.loads(candidate['coverage'] or '{}').get('scope_mode', 'directory')
    if mode not in ('directory', 'site'):
        return
    key = 'selection_context:' + candidate['id']
    if store.get(key, {}).get('version') == VERSION:
        return
    used, parents, entries = 0, set(), []
    current = capture_scope(candidate['url'], 'directory')
    for capture in captures:
        page = EvidencePage(capture['url'])
        page.feed(decode((store.root/capture['path']).read_bytes(), capture.get('content_type') or '')[0])
        size = len('\n'.join(page.text))
        used += size
        # Follow at most two same-scope frame/entry targets. This is evidence
        # recovery, never an unbounded crawl or permission to change accounts.
        entries.extend(page.frames)
        if size <= 1200:
            entries.extend(link['url'] for link in page.links if re.search(
                r'enter|continue|click\s*here|new\s+(?:home|site|url|location)',
                link['anchor']+' '+link['context'], re.I))
        # An actual source link can identify a wider EQ subsite homepage. Never
        # guess arbitrary ancestor paths or cross a shared-host account.
        for link in page.links:
            boundary = capture_scope(link['url'], 'directory')
            if (boundary != owner and boundary != current and not urlsplit(link['url']).query
                    and within_capture_scope(boundary, owner) and within_capture_scope(current, boundary)
                    and home_source(link['url'], boundary)):
                parents.add(boundary)
    homes = [] if has_home else sorted(parents, key=lambda u:len(urlsplit(u).path))[:1] + [owner]
    known = {original_url(c['url']) for c in captures}
    entries = list(dict.fromkeys(u for u in entries if original_url(u) not in known
        and within_capture_scope(u, candidate['scope']) and within_capture_scope(u, owner)))[:2]
    requests = [(url, None) for url in entries] + [(url, url) for url in homes]
    level = captures[0]['tier']
    start, end = TIERS[level]
    added, errors = 0, []
    for url, boundary in requests:
        try:
            listing = downloader.call({'op':'list', 'url':url, 'from':start, 'to':end})
            records = sorted(listing['captures'], key=lambda r:r['timestamp'])
            # Near the graded candidate's era, plus a later homepage when
            # available: ownership/game changes are relevant scope evidence.
            chosen = sorted(records, key=lambda r:abs(int(r['timestamp'])-int(captures[0]['timestamp'])))[:1]
            if boundary and records and records[-1] not in chosen:
                chosen.append(records[-1])
            for record in chosen:
                identity = home_source(record['url'], boundary) if boundary else original_url(record['url']) == original_url(url)
                if not identity or not in_capture_window(record['timestamp'], {'from':start,'to':end}):
                    raise CrawlError('Homepage evidence changed its account or date boundary')
                suffix = digest(url)[:12]
                destination = store.root/'captures'/candidate['id']/(record['timestamp']+'-home-'+suffix+'.html')
                result = downloader.call({'op':'capture', 'url':record['url'], 'timestamp':record['timestamp'],
                                          'from':start, 'to':end, 'destination':str(destination)})
                if original_url(result['url']) != original_url(record['url']) or not in_capture_window(result['timestamp'], {'from':start,'to':end}):
                    raise CrawlError('Homepage capture failed exact URL/date validation')
                raw = destination.read_bytes()
                if digest(raw) != result['sha256'] or len(raw) != result['bytes']:
                    raise CrawlError('Homepage source hash changed')
                text, encoding = decode(raw, result.get('content_type') or '')
                page = Page(result['url']);page.feed(text)
                size = len('\n'.join(page.text))
                # Optional context cannot push useful candidate text over the
                # complete-source grading limit. Never crop source evidence.
                if not size or used + size > 120000:
                    continue
                result.update(path=str(destination.relative_to(store.root)), tier=level,
                              cdx_digest=record['digest'], cdx_length=record['length'],
                              retrieved_at=now(), encoding=encoding, title=' '.join(page.title),
                              source='wayback', site_coverage='selection_context',
                              selection_role='site_home' if boundary else 'supporting_page')
                if boundary:result['selection_scope'] = boundary
                captures.append(result);used += size;added += 1
                store.db.execute('UPDATE candidates SET captures=? WHERE id=?', (json.dumps(captures),candidate['id']))
                store.db.commit()
        except TransportUnavailable:
            raise
        except CrawlError as error:
            # Missing context grants no wider scope. Keep useful candidate
            # text and the original cumulative transport/spend allowances.
            errors.append(str(error))
    store.set(key, {'version':VERSION,'status':'sampled' if added else 'unavailable',
                    'captures':added, **({'reason':errors[0]} if errors else {})})
