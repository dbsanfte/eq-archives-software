"""Conservative site boundaries, generous acquisition inside an EQ community."""
import re
from urllib.parse import urlsplit

from common import site_scope, within_capture_scope

VERSION = 'eq-site-selection-v2'
SITE_TYPES = ('dedicated_eq', 'mixed', 'unknown')
CONTENT_KINDS = ('substantive', 'directory', 'identity_only', 'unavailable', 'unrelated')


def home_source(url, owner=None):
    """Only a site's/account's landing page can establish its wider boundary."""
    owner = owner or site_scope(url)
    if not within_capture_scope(url, owner):
        return False
    page, boundary = urlsplit(url), urlsplit(owner)
    if page.query:
        return False
    path = page.path.rstrip('/')
    base = boundary.path.rstrip('/')
    return path == base or bool(re.fullmatch(
        re.escape(base) + r'/(?:index|default)\.(?:html?|shtml|php|asp|aspx)', path, re.I))


def automatic_scope(row, documents):
    """Never infer a parent site's subject from an EQ page beneath it."""
    if row['scope_mode'] != 'directory':
        return row['scope'], row['scope_mode']
    owner = site_scope(row['url'])
    rating = row['rating']
    verified = []
    if rating.get('site_type') == 'dedicated_eq' and rating.get('confidence') == 'high':
        for evidence in rating.get('scope_evidence', []):
            boundary = evidence_boundary(documents[evidence['slot']])
            if (boundary and within_capture_scope(boundary, owner)
                    and within_capture_scope(row['url'], boundary)):
                verified.append(boundary)
    if row['scope'] == owner:
        return owner, 'site'
    if verified:
        boundary = min(verified, key=lambda url:len(urlsplit(url).path))
        return (owner, 'site') if within_capture_scope(owner, boundary) else (boundary, 'directory')
    return row['scope'], 'directory'


def evidence_boundary(document):
    owner = site_scope(document['url'])
    boundary = document.get('scope', owner) if document.get('role') == 'site_home' else owner
    if within_capture_scope(boundary, owner) and home_source(document['url'], boundary):
        return boundary
    return None


def automatic_reason(rating, minimum):
    if rating.get('confidence') != 'high':
        return 'The EQ assessment needs higher confidence before automatic approval.'
    if rating['grade'] < minimum:
        return f"Selection grade {rating['grade']} is below the configured minimum {minimum}."
    return None
