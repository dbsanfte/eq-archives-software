"""Single-site submissions use the ordinary coverage, sampling and grading path."""

import json
import os
import re
from urllib.parse import urlsplit

from common import CrawlError, capture_scope, digest, now, original_url, site_identity
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
        return result
    except ValueError:
        raise CrawlError('Enter a public HTTP(S) website URL or a Wayback link to its original page') from None


def existing_site(store, url):
    identity = site_identity(url)
    return next((row for row in store.candidates() if site_identity(row['url']) == identity), None)


def prepare(run, operation):
    """Check only this account's Git metadata; no link spider or archive walk."""
    target = operation['payload']['target']
    url = target['url']
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
    evidence = [{'kind': 'manual_submission', 'source_url': target['submitted_url'], 'original_url': url,
                 'submitted_at': operation.get('created') or now()}]
    run.db.execute('INSERT OR IGNORE INTO candidates(id,url,scope,priority,coverage,evidence) VALUES (?,?,?,?,?,?)',
                   (identifier, url, capture_scope(url), 100, json.dumps(coverage), json.dumps(evidence)))
    run.db.execute('UPDATE candidates SET coverage=? WHERE id=?', (json.dumps(coverage), identifier))
    if check['status'] == 'already_archived':
        run.db.execute("UPDATE candidates SET state='already_archived' WHERE id=?", (identifier,))
    run.db.commit()
    run.set('discovery_result', {'candidates': 1, 'source': 'manual', 'url': url})
    return check['status'] == 'new_site'
