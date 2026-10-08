"""Ezboard read-only URL grammar and source-backed board membership.

A forum token concatenates the board and forum names without a separator. A
prefix alone therefore cannot distinguish `eqasylum/general` from a different
board called `eqasylumgeneral`. Require archived navigation evidence as well.
"""
import re
from urllib.parse import parse_qs, urlsplit

from common import CrawlError, Page, decode, original_url

SHARD = re.compile(r'(?:www\.)?(?:server|pub|p|b)[0-9]+\.ezboard\.com\Z', re.I)
NAME = re.compile(r'[a-zA-Z0-9_]{1,120}\Z')
READ_ACTIONS = {'showMessage', 'showMessageRange', 'showNextMessage', 'showPrevMessage'}
CATALOG_KINDS = ('b', 'f')


def catalog_url(board, query):
    return 'http://' + query['host'] + '/' + query['kind'] + board


def catalog_member(item, query):
    return item['host'] == query['host'] and item['token'][0] == query['kind']


def shard(host):
    return bool(SHARD.fullmatch(host))


def address(value):
    url = original_url(value)
    if not url:
        return None
    parsed = urlsplit(url)
    if not shard(parsed.netloc):
        return None
    path = parsed.path.strip('/')
    if '/' in path:
        return None
    stem, dot, action = path.partition('.')
    if dot and action in ('htm', 'html'):
        action = ''
    if not stem or stem[0] not in ('b', 'f') or not NAME.fullmatch(stem[1:]):
        return None
    if action and (stem[0] != 'f' or action not in READ_ACTIONS):
        return None
    if action and not parse_qs(parsed.query).get('topicID'):
        return None
    return {'url': url, 'host': parsed.netloc, 'token': stem, 'action': action,
            'kind': 'board' if stem[0] == 'b' else 'message' if action else 'forum'}


def board_name(url):
    parsed = address(url)
    if not parsed or parsed['kind'] != 'board':
        raise CrawlError('Use an Ezboard board URL such as http://pub4.ezboard.com/beqasylum')
    return parsed['token'][1:]


def candidate(url, board):
    parsed = address(url)
    if not parsed:
        return None
    if parsed['kind'] == 'board':
        return parsed if parsed['token'] == 'b' + board else None
    return parsed if parsed['token'].startswith('f' + board) and len(parsed['token']) > len(board) + 1 else None


def source_page(data, url, content_type=''):
    page = Page(url)
    text, encoding = decode(data, content_type)
    page.feed(text)
    return page, encoding


def source_problem(page):
    title = ' '.join(page.title).strip().lower()
    if title.startswith('system message:') or title in ('ezboard - error', 'ezboard error'):
        return 'Ezboard system/error page; discussion was not recovered'
    if not ' '.join(page.text).strip():
        return 'Capture contains no readable source text'
    return None


def belongs(page, url, board, forums=()):
    parsed = candidate(url, board)
    if not parsed or source_problem(page):
        return False
    # Only platform navigation endpoints declare ownership. Arbitrary external
    # links with a boardName query are not evidence about this page.
    declarations = set()
    first_board = None
    for link in page.links:
        target = original_url(link['url'])
        parts = urlsplit(target)
        if shard(parts.netloc) and parts.path in ('/BBSForum.showForumSearch',
                '/BBSSystem.handleLoginCheck', '/BBSMessageBoard.showRegistrationChoice'):
            declarations.update(parse_qs(parts.query).get('boardName', []))
        linked = address(target)
        if linked and linked['kind'] == 'board' and first_board is None:
            first_board = linked['token'][1:]
    if declarations and declarations != {board}:
        return False
    if first_board is not None and first_board != board:
        return False
    if parsed['kind'] == 'board':
        return True
    # The first board link is Ezboard's breadcrumb in the examined captures.
    # Reject a conflicting breadcrumb even for a previously verified forum.
    return (first_board == board or declarations == {board} or parsed['token'] in forums)


def forum_links(page, url, board):
    """Only a board index can establish forum membership without a breadcrumb."""
    current = candidate(url, board)
    if not current or current['kind'] != 'board':
        return []
    return sorted({item['token'] for link in page.links
                   if (item := candidate(link['url'], board)) and item['kind'] == 'forum'})


def board_url(url):
    item = address(url)
    if not item or item['kind'] != 'board':
        return None
    parsed = urlsplit(item['url'])
    return parsed.scheme + '://' + parsed.netloc + '/' + item['token']


def parent_from_page(page, url):
    item = address(url)
    if not item or source_problem(page):
        return None
    if item['kind'] == 'board':
        return board_url(url) if belongs(page, url, item['token'][1:]) else None
    for link in page.links:
        parent = board_url(link['url'])
        if parent and belongs(page, url, board_name(parent)):
            return parent
    return None


def remember_page(store, page, source):
    parent = parent_from_page(page, source['url'])
    if not parent:
        return None
    board = board_name(parent)
    item = address(source['url'])
    tokens = forum_links(page, source['url'], board) + [item['token']]
    proof = json_proof(source)
    for token in tokens:
        previous = store.db.execute('SELECT board FROM ezboard_aliases WHERE token=?', (token,)).fetchone()
        if previous and previous['board'] != board:
            raise CrawlError('Archived Ezboard navigation gives conflicting board identities')
        store.db.execute('INSERT OR IGNORE INTO ezboard_aliases VALUES (?,?,?,?)', (token, board, parent, proof))
    store.db.commit()
    return parent


def json_proof(source):
    import json
    return json.dumps({key: source[key] for key in ('url', 'timestamp', 'sha256')})


def candidate_url(store, url):
    """None means an Ezboard deep link still needs source-backed resolution."""
    item = address(url)
    if not item:
        # A historical port does not turn an Ezboard profile/form into a
        # standalone website. Capture still requires a supported read URL.
        return None if shard(urlsplit(url).hostname or '') else url
    if item['kind'] == 'board':
        return board_url(url)
    owner = store.db.execute('SELECT url FROM ezboard_aliases WHERE token=?', (item['token'],)).fetchone()
    return owner['url'] if owner else None
