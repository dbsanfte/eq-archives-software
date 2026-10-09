"""Bounded approved-site traversal using one persistent Wayback downloader."""

from collections import deque
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from acquisition import Downloader
from archive_layout import archive_path
from common import CAPTURE_WINDOW, CrawlError, Page, Store, decode, digest, in_capture_window, now, original_url, save, tier, within_scope
from discovery import SKIP
from indexer.capture_enrichment import DEFAULT_POLICY

LIMITS = {"sites": 5, "pages_per_site": 20, "files": 100, "bytes": 64 * 1024 * 1024,
          "requests": 500, "seconds": 1800, "page_bytes": 1024 * 1024}


class CaptureBound(CrawlError):
    pass


def verified_path(root, capture):
    relative = Path(capture['path'])
    base = Path(root).resolve()
    path = base / relative
    if relative.is_absolute() or any(part == '..' for part in relative.parts) or any(
            item.is_symlink() for item in (path, *path.parents) if item != base and base in item.parents):
        raise CrawlError("Source path escaped staging")
    path = path.resolve()
    if base not in path.parents:
        raise CrawlError("Source path escaped staging")
    try:
        if path.stat().st_size != capture["bytes"] or capture['bytes'] < 0:
            raise CrawlError("Staged source size changed or exceeds the page limit")
        checksum = hashlib.sha256()
        with path.open('rb') as source:
            while chunk := source.read(1024 * 1024):
                checksum.update(chunk)
    except OSError:
        raise CrawlError("Staged source is unavailable") from None
    if checksum.hexdigest() != capture["sha256"] or not original_url(capture["url"]) or not tier(capture["timestamp"]):
        raise CrawlError("Staged capture failed source integrity checks")
    return path


TEXT_LIMIT = 32 * 1024 * 1024


def verified_source(root, capture):
    path = verified_path(root, capture)
    if capture['bytes'] > TEXT_LIMIT:
        raise CrawlError('This file is too large for the text reader; download the complete original file instead')
    return path.read_bytes()


def readable(capture):
    return (capture.get('content_type') or capture.get('mimetype') or 'text/html').split(';')[0].strip().lower() in (
        'text/html', 'application/xhtml+xml', 'text/plain')


def indexable(capture):
    return (not capture.get('supporting_source') and readable(capture)
            and 0 < capture['bytes'] <= TEXT_LIMIT and capture.get('has_text', True))


def describe_source(root, capture):
    result = {'kind': 'page' if readable(capture) else 'file'}
    if readable(capture) and capture['bytes'] <= TEXT_LIMIT:
        page, encoding = document(root, capture)
        result.update(title=' '.join(page.title), encoding=encoding, has_text=bool(' '.join(page.text).strip()))
    return result


def document(root, capture):
    raw = verified_source(root, capture)
    text, encoding = decode(raw, capture.get("content_type") or "")
    page = Page(capture["url"])
    if (capture.get('content_type') or capture.get('mimetype') or '').split(';')[0] == 'text/plain':
        page.text = [text]
    else:
        page.feed(text)
    return page, encoding


def manifest_hash(manifest):
    return digest(manifest)


def check_manifest(root, manifest):
    sites = {site["id"]: site for site in manifest.get("sites", [])}
    ezboard = any(site.get('scope_mode') == 'ezboard' for site in sites.values())
    sitepowerup = any(site.get('scope_mode') == 'sitepowerup' for site in sites.values())
    limits = LIMITS
    from full_capture import POLICY, allowed as scope_allowed
    complete = manifest.get('capture_policy') == POLICY
    if complete:
        limits = {**LIMITS, 'files': float('inf'), 'bytes': float('inf')}
        if manifest.get('capture_window') != CAPTURE_WINDOW or any(
                manifest.get('capture_coverage', {}).get(site, {}).get('state') not in ('complete', 'complete_with_gaps') for site in sites):
            raise CrawlError('Complete file capture has unfinished inventory or downloads')
    board, forums = None, set()
    if sitepowerup:
        import sitepowerup as platform
        from sitepowerup_capture import LIMITS as BOARD_LIMITS
        if len(sites) != 1:
            raise CrawlError('Review each SitePowerUp board as one independent capture')
        site = next(iter(sites.values()))
        board = platform.board_name(site['scope'])
        if platform.board_name(site['url']) != board or platform.board_url(site['scope']) != site['scope']:
            raise CrawlError('SitePowerUp capture scope changed')
        if not complete:
            limits = {**LIMITS, 'files': BOARD_LIMITS['max_captures'], 'bytes': BOARD_LIMITS['max_bytes']}
    if ezboard:
        from ezboard_portal import PORTAL_LIMITS
        from ezboard import board_name, board_url, belongs, candidate, forum_links
        if len(sites) != 1:
            raise CrawlError('Review each Ezboard as one independent capture')
        site = next(iter(sites.values()))
        board = board_name(site['scope'])
        if board_name(site['url']) != board or board_url(site['scope']) != site['scope']:
            raise CrawlError('Ezboard capture scope changed')
        if not complete:
            limits = {**LIMITS, 'files': PORTAL_LIMITS['max_captures'], 'bytes': PORTAL_LIMITS['max_bytes']}
    if manifest.get("schema") != 1 or not 1 <= len(manifest.get("captures", [])) <= limits["files"]:
        raise CrawlError("Invalid capture batch")
    if not 1 <= len(sites) <= LIMITS["sites"]:
        raise CrawlError("Invalid approved site set")
    if ezboard:
        for capture in manifest['captures']:
            if capture.get('supporting_source'):
                continue
            parsed = candidate(capture['url'], board)
            if parsed and parsed['kind'] in ('board', 'forum'):
                page, _ = document(root, capture)
                if belongs(page, capture['url'], board):
                    forums.update(forum_links(page, capture['url'], board))
                    if parsed['kind'] == 'forum':
                        forums.add(parsed['token'])
    seen, size = set(), 0
    identities = {(c['url'], c['timestamp'], c['sha256']): c for c in manifest['captures']}
    verified = set()
    deferred = []
    for capture in manifest["captures"]:
        verified_path(root, capture)
        if not complete and capture['bytes'] > LIMITS['page_bytes']:
            raise CrawlError('Staged source size changed or exceeds the page limit')
        if manifest.get('capture_window') and not in_capture_window(capture['timestamp'], manifest['capture_window']):
            raise CrawlError('Capture is outside the saved date window')
        site = sites.get(capture.get("candidate_id"))
        if complete and capture.get('supporting_source'):
            # Check this graph after validating every primary page. Cycles,
            # invented parents and cross-candidate references cannot grant scope.
            deferred.append(capture)
            permitted = site is not None
        elif ezboard:
            page, _ = document(root, capture)
            permitted = site is not None and belongs(page, capture['url'], board, forums)
        elif sitepowerup:
            page, _ = platform.source_page(verified_source(root, capture), capture['url'], capture.get('content_type') or '')
            permitted = site is not None and platform.belongs(page, capture['url'], board)
        elif complete:
            permitted = site is not None and scope_allowed(capture['url'], site)
        else:
            permitted = (original_url(capture["url"]) == original_url(site["url"]) if site and site.get("scope_mode") == "page"
                       else within_scope(capture["url"], site["scope"]) if site else False)
        if not permitted:
            raise CrawlError("Capture is outside the approved site scope")
        if capture.get("archive_path") != archive_path(capture) or capture["archive_path"] in seen:
            raise CrawlError("Invalid or duplicate archive destination")
        seen.add(capture["archive_path"])
        if not capture.get('supporting_source'):
            verified.add((capture['url'], capture['timestamp'], capture['sha256']))
        size += capture["bytes"]
    reference_cache = {}
    while deferred:
        pending = []
        for capture in deferred:
            source = capture['supporting_source']
            key = tuple(source.get(field) for field in ('url', 'timestamp', 'sha256'))
            if key not in verified:
                pending.append(capture)
                continue
            parent = identities[key]
            if key not in reference_cache:
                from source_assets import references
                reference_cache[key] = references(root, parent)
            if parent['candidate_id'] != capture['candidate_id'] or original_url(capture['url']) not in reference_cache[key]:
                raise CrawlError('Supporting file is not referenced by its verified source')
            verified.add((capture['url'], capture['timestamp'], capture['sha256']))
        if len(pending) == len(deferred):
            raise CrawlError('Supporting file has no verified source within the approved capture')
        deferred = pending
    if size > limits["bytes"]:
        raise CrawlError("Batch exceeds the source byte limit")


def capture_sites(root, batch_id, sites, downloader_factory=Downloader, progress=None):
    if any(site.get('scope_mode') == 'sitepowerup' for site in sites):
        if len(sites) != 1:
            raise CrawlError('Capture each whole SitePowerUp board independently')
        from sitepowerup_portal import capture_board
        return capture_board(root, batch_id, sites[0], downloader_factory, progress)
    if any(site.get('scope_mode') == 'ezboard' for site in sites):
        if len(sites) != 1:
            raise CrawlError('Capture each whole Ezboard independently')
        from ezboard_portal import capture_board
        return capture_board(root, batch_id, sites[0], downloader_factory, progress)
    from full_capture import POLICY, capture
    if sites and all(site.get('capture_policy') == POLICY for site in sites):
        return capture(root, batch_id, sites, downloader_factory, progress)
    directory = Path(root) / "batches" / batch_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    draft_path = directory / "draft.json"
    if draft_path.exists():
        draft = json.loads(draft_path.read_text())
    else:
        draft = {"schema": 1, "batch_id": batch_id, "created_at": now(), "limits": LIMITS,
                 "sites": sites, "captures": [], "visited": [], "notes": [], "indexing": dict(DEFAULT_POLICY)}
        for site in sites:
            for capture in site["captures"]:
                if not in_capture_window(capture['timestamp']):
                    continue
                verified_source(root, capture)
                path = archive_path(capture)
                existing = next((item for item in draft["captures"] if item["archive_path"] == path), None)
                if existing:
                    if original_url(existing["url"]) != original_url(capture["url"]) or existing["sha256"] != capture["sha256"]:
                        raise CrawlError("Sample sources collide in the existing archive layout")
                    continue
                draft["captures"].append({**capture, "candidate_id": site["id"], "archive_path": path})
        save(draft_path, draft)
    # A paused legacy traversal may have marked a URL finished after one sample.
    # Revisit its catalogs, retaining sources and the original transport budget.
    # Already completed review/publication manifests never pass through here.
    if not draft.get('capture_window'):
        draft['capture_window'] = dict(CAPTURE_WINDOW)
        draft['captures'] = [c for c in draft['captures'] if in_capture_window(c['timestamp'])]
        draft['visited'] = []
        draft['catalogs'] = {}
        draft['capture_coverage'] = {}
        save(draft_path, draft)
    if draft['capture_window'] != CAPTURE_WINDOW:
        raise CrawlError('Saved capture date policy changed')
    catalogs = draft['catalogs']
    coverage = draft['capture_coverage']
    # Each batch owns a separate durable, cumulative HTTP budget.
    transport_store = Store(directory)
    downloader = None
    seen = {(c["candidate_id"], original_url(c["url"]), c['timestamp']) for c in draft["captures"]}
    visited = {tuple(entry) for entry in draft["visited"]}
    site_index, site_url = 0, None
    def report(phase, current_url=None):
        if progress:
            checked = sum(c.get('checked', c['offset']) for c in catalogs.values())
            pending = sum(len(c['records']) - c['offset'] for c in catalogs.values())
            progress({"phase": phase, "files": len(draft["captures"]),
                      "bytes": sum(c["bytes"] for c in draft["captures"]), "urls_checked": len(visited),
                      "sites_done": site_index if phase == 'ready_for_review' else max(0, site_index - 1),
                      "sites_total": len(sites), "site_url": site_url, "current_url": current_url,
                      "capture_window": draft['capture_window'],
                      "versions_found": checked + pending, "versions_pending": pending,
                      "catalogs_pending": sum(not c['end'] for c in catalogs.values())})
    report('preparing')
    args = SimpleNamespace(delay=0, bytes_per_second=0, max_requests=LIMITS["requests"],
                           max_page_bytes=LIMITS["page_bytes"], max_bytes=LIMITS["bytes"], max_seconds=LIMITS["seconds"])
    def acquire(job):
        try:
            return downloader.call(job)
        except CrawlError as error:
            if job['op'] == 'capture' and str(error).startswith(("Wayback HTTP 404", "Wayback HTTP 410", "Response exceeds byte limit", "Expanded response exceeds byte limit")):
                note(job['url'], 'Capture ' + job['timestamp'] + ': ' + str(error) + '; version excluded')
                return None
            raise
    def note(url, detail):
        item = {'url': url, 'note': detail}
        if item not in draft['notes']:
            draft['notes'].append(item)
    try:
        for site_index, site in enumerate(sites, 1):
            site_url = site['url']
            coverage[site['id']] = {'state': 'capturing', 'reason': 'Reading all dated versions in the approved scope'}
            frontier = deque([site["scope"], site["url"]])
            for capture in site.get('reviewed_captures', []):
                page, _ = document(root, capture)
                frontier.extend(link['url'] for link in page.links)
            for capture in draft["captures"]:
                if capture["candidate_id"] == site["id"]:
                    frontier.append(capture['url'])
                    page, _ = document(root, capture)
                    frontier.extend(link["url"] for link in page.links)
            checked = set()
            while frontier:
                url = original_url(frontier.popleft())
                allowed = url == original_url(site["url"]) if site.get("scope_mode") == "page" else within_scope(url, site["scope"]) if url else False
                if not url or url in checked or not allowed or SKIP.search(urlsplit(url).path):
                    continue
                checked.add(url)
                if (site["id"], url) in visited:
                    continue
                # Count attempts, including unavailable URLs, to keep traversal finite.
                key = digest([site['id'], url])
                attempted = sum(c['site'] == site['id'] for c in catalogs.values())
                if key not in catalogs:
                    if attempted >= LIMITS['pages_per_site']:
                        raise CaptureBound('URL traversal limit reached; full date coverage remains incomplete')
                    catalogs[key] = {'site': site['id'], 'url': url, 'records': [], 'offset': 0, 'resume_key': None, 'end': False}
                catalog = catalogs[key]
                # Old paused captures already have an offset into their saved
                # page. Carry checked counts across subsequent CDX pages.
                catalog.setdefault('checked', catalog['offset'])
                while True:
                    if catalog['offset'] >= len(catalog['records']):
                        if catalog['end']:
                            break
                        report('checking_wayback', url)
                        downloader = downloader or downloader_factory(transport_store, args)
                        listing = acquire({'op': 'capture_list', 'url': url, 'from': CAPTURE_WINDOW['from'],
                                           'to': CAPTURE_WINDOW['to'], 'resume_key': catalog['resume_key']})
                        if listing is None:
                            catalog.update(records=[], offset=0, end=True)
                            break
                        resume = listing.get('resume_key')
                        if resume and resume == catalog['resume_key']:
                            raise CrawlError('CDX repeated its continuation key; date coverage is unresolved')
                        catalog.update(records=listing['captures'], offset=0, resume_key=resume, end=not resume)
                        save(draft_path, draft)
                        continue
                    record = catalog['records'][catalog['offset']]
                    if original_url(record['url']) != url or not in_capture_window(record['timestamp']):
                        raise CrawlError('Capture listing changed original URL or date window')
                    if (site['id'], url, record['timestamp']) not in seen:
                        if len(draft['captures']) >= LIMITS['files']:
                            raise CaptureBound('Capture file limit reached; full date coverage remains incomplete')
                        if sum(c['bytes'] for c in draft['captures']) >= LIMITS['bytes'] - LIMITS['page_bytes']:
                            raise CaptureBound('Batch source byte budget reached')
                        capture = catalog.get('receipt')
                        if not capture:
                            folder = directory / 'captures' / site['id']
                            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                            destination = folder / (digest(url) + '-' + record['timestamp'] + '.html')
                            report('downloading', record['url'])
                            downloader = downloader or downloader_factory(transport_store, args)
                            capture = acquire({'op': 'capture', 'url': record['url'], 'timestamp': record['timestamp'],
                                               'from': CAPTURE_WINDOW['from'], 'to': CAPTURE_WINDOW['to'], 'destination': str(destination)})
                            if capture:
                                capture.update(path=str(destination.relative_to(root)), candidate_id=site['id'],
                                               requested_timestamp=record['timestamp'], retrieved_at=now())
                                catalog['receipt'] = capture
                                save(draft_path, draft)
                        if capture:
                            if original_url(capture['url']) != url or not in_capture_window(capture['timestamp']):
                                raise CrawlError('Downloaded source changed original URL or date window')
                            capture.update(tier=tier(capture['timestamp']), source='wayback', cdx_digest=record['digest'],
                                           cdx_length=record['length'], site_coverage='bounded_all_versions')
                            page, encoding = document(root, capture)
                            capture.update(encoding=encoding, title=' '.join(page.title), archive_path=archive_path(capture))
                            collision = next((item for item in draft['captures'] if item['archive_path'] == capture['archive_path']), None)
                            if collision:
                                if original_url(collision['url']) != url or collision['sha256'] != capture['sha256']:
                                    note(url, 'Excluded: archive filename collides with another capture')
                            elif ' '.join(page.text).strip():
                                draft['captures'].append(capture)
                                seen.add((site['id'], url, capture['timestamp']))
                                frontier.extend(link['url'] for link in page.links)
                            else:
                                note(url, 'No readable source text')
                    catalog['offset'] += 1
                    catalog['checked'] += 1
                    catalog.pop('receipt', None)
                    save(draft_path, draft)
                visited.add((site["id"], url))
                draft["visited"] = sorted(visited)
                save(draft_path, draft)
                report('checking_wayback')
            coverage[site['id']] = {'state': 'complete', 'reason': 'All listed 1999–2006 versions of discovered pages checked; Wayback may have missing pages or captures'}
    except CrawlError as error:
        if not isinstance(error, CaptureBound) and "budget" not in str(error).lower():
            raise
        note(url, str(error) + '; bounded subset staged for review')
        for pending_site in sites[site_index - 1:]:
            coverage[pending_site['id']] = {'state': 'bounded', 'reason': str(error) + '; capture of the full 1999–2006 window is incomplete'}
    finally:
        if downloader:
            downloader.close()
        draft["transport"] = transport_store.get("wayback_transport", {})
        save(draft_path, draft)
        transport_store.close()
    # Completed production captures enter source-bound automatic indexing.
    manifest = {key: value for key, value in draft.items() if key != 'catalogs'}
    check_manifest(root, manifest)
    report('ready_for_review')
    return manifest
