"""Single-site submissions use the ordinary coverage, sampling and grading path."""

import json
import os
import re
from urllib.parse import urlsplit

from common import CrawlError, candidate_exclusion, capture_scope, digest, now, original_url, site_identity
from discovery import Archive
from site_inventory import SiteInventory


def site_url(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4096:
        raise CrawlError('Enter a website URL or a Wayback Machine link (up to 4096 characters)')
    value = value.strip()
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise CrawlError('Website URLs must not contain whitespace or control characters')
    if not re.match(r'^[a-z][a-z0-9+.-]*:', value, re.I):
        value = 'http://' + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            raise ValueError()
        if parsed.hostname == 'web.archive.org':
            if parsed.port not in (None, 80, 443):
                raise ValueError()
            replay = re.fullmatch(r'/web/(?:\d{1,14}\*?|\*)(?:[a-z]+_)?/(.+)', parsed.path, re.I)
            if not replay:
                raise ValueError()
            value = replay[1] + ('?' + parsed.query if parsed.query else '')
            if not value.lower().startswith(('http://', 'https://')):
                value = 'http://' + value
        result = original_url(value)
        if not result or urlsplit(result).hostname == 'web.archive.org':
            raise ValueError()
        if reason := candidate_exclusion(result):
            raise CrawlError(reason)
        return result
    except ValueError:
        raise CrawlError('Enter a public HTTP(S) website URL or a Wayback link to its original page') from None


def existing_site(store, url):
    from ezboard import candidate_url
    url = candidate_url(store, url)
    if not url:
        return None
    identity = site_identity(url, store)
    return next((row for row in store.candidates() if row['state'] != 'duplicate_candidate' and site_identity(row['url'], store) == identity), None)


def prepare(run, operation, downloader=None):
    """Check only this account's Git metadata; no link spider or archive walk."""
    target = operation['payload']['target']
    url = target['url']
    if reason := candidate_exclusion(url):
        raise CrawlError(reason)
    from sitepowerup import shard, board_url as sitepowerup_board
    if shard(urlsplit(url).netloc):
        url = sitepowerup_board(url)
        if not url:
            raise CrawlError('Use a SitePowerUp board or message URL with one numeric BoardID; no Luna call was made.')
    from ezboard import board_url, candidate_url
    from ezboard_discovery import resolve
    resolved = candidate_url(run, url)
    if resolved is None:
        if downloader is None:
            raise CrawlError('Ezboard parent-board evidence is required before creating a candidate')
        resolved = resolve(run, url, downloader)
        if not resolved:
            raise CrawlError('Could not verify the parent Ezboard. Submit its top-level b… board URL; no Luna call was made.')
    url = resolved
    repository = os.environ.get('ARCHIVE_REPO')
    if not repository:
        raise CrawlError('Archive metadata is not configured; the site check cannot continue')
    check = SiteInventory(Archive(repository, run)).check(url, force=True)
    if check['status'] == 'inventory_partial' or not check.get('complete'):
        progress = check.get('progress') or {}
        detail = f" {progress['checked']} of {progress['total']} snapshots checked." if progress else ''
        raise CrawlError('Archive coverage is still unverified.' + detail + ' ' + check.get('message', 'Resume the site check to continue.'))
    identifier = digest(url)[:24]
    coverage = {'status': check['status'], 'site_check': check, 'manual_operation': operation['id']}
    if board_url(url):
        coverage['scope_mode'] = 'ezboard'
    elif sitepowerup_board(url):
        coverage['scope_mode'] = 'sitepowerup'
    evidence = [{'kind': 'manual_submission', 'source_url': target['submitted_url'], 'original_url': url,
                 'submitted_at': operation.get('created') or now(), 'linked_url': target['url']}]
    run.db.execute('INSERT OR IGNORE INTO candidates(id,url,scope,priority,coverage,evidence) VALUES (?,?,?,?,?,?)',
                   (identifier, url, capture_scope(url, coverage.get('scope_mode', 'directory')), 100, json.dumps(coverage), json.dumps(evidence)))
    run.db.execute('UPDATE candidates SET coverage=? WHERE id=?', (json.dumps(coverage), identifier))
    if check['status'] == 'new_site':
        from ezboard_discovery import evidence as parent_evidence
        captures = parent_evidence(run, target['url'], url)
        if captures:
            run.db.execute("UPDATE candidates SET captures=?,state='sampled' WHERE id=? AND captures='[]'", (json.dumps(captures), identifier))
    if check['status'] == 'already_archived':
        run.db.execute("UPDATE candidates SET state='already_archived' WHERE id=?", (identifier,))
    run.db.commit()
    run.set('discovery_result', {'candidates': 1, 'source': 'manual', 'url': url})
    return check['status'] == 'new_site'
