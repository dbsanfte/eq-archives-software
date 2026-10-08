"""SitePowerUp's ASP query grammar and source-verified board membership."""
import re
from urllib.parse import parse_qsl, urlsplit

from common import CrawlError, Page, decode, original_url

CATALOG_KINDS = ('display', 'default', 'reply')
NUMBER = re.compile(r'[1-9][0-9]{0,17}\Z')


def shard(host):
    return host.lower() in ('sitepowerup.com', 'www.sitepowerup.com')


def address(value):
    url = original_url(value)
    if not url:
        return None
    parsed = urlsplit(url)
    if not shard(parsed.netloc) or parsed.path.lower() != '/mb/view.asp':
        return None
    fields = {}
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        key = key.lower()
        if key in fields:  # ASP's treatment of duplicate parameters is ambiguous.
            return None
        fields[key] = item
    board = fields.get('boardid', '')
    if not NUMBER.fullmatch(board):
        return None
    action = fields.get('action', 'display').lower()
    reply = fields.get('reply', '')
    if action == 'reply' and not NUMBER.fullmatch(reply):
        return None
    return {'url': url, 'host': parsed.netloc, 'board': board, 'action': action,
            'reply': reply, 'kind': 'board' if action == 'display' else 'message' if action == 'reply' else 'form'}


def board_name(url):
    item = address(url)
    if not item:
        raise CrawlError('Use a SitePowerUp /mb/view.asp URL with one numeric BoardID')
    return item['board']


def board_url(url):
    item = address(url)
    if not item:
        return None
    return 'http://www.sitepowerup.com/mb/view.asp?Action=Display&BoardID=' + item['board']


def candidate_url(url):
    if not shard(urlsplit(url).netloc):
        return url
    item = address(url)
    return board_url(url) if item and item['kind'] in ('board', 'message') else None


def candidate(url, board):
    item = address(url)
    return item if item and item['board'] == board and item['kind'] in ('board', 'message') else None


class SourcePage(Page):
    def __init__(self, url):
        super().__init__(url)
        self.board_fields, self.reply_fields = set(), set()

    def handle_starttag(self, tag, attributes):
        super().handle_starttag(tag, attributes)
        fields = dict(attributes)
        if tag == 'input' and fields.get('type', '').lower() == 'hidden':
            name, value = fields.get('name', '').lower(), fields.get('value', '')
            if name == 'boardid':
                self.board_fields.add(value)
            elif name == 'reply':
                self.reply_fields.add(value)


def source_page(data, url, content_type=''):
    text, encoding = decode(data, content_type)
    page = SourcePage(url)
    page.feed(text)
    return page, encoding


def source_problem(page):
    if not ' '.join(page.text).strip():
        return 'Capture contains no readable source text'
    title = ' '.join(page.title).strip().lower()
    if title in ('error', 'sitepowerup - error', 'sitepowerup.com - error', 'sitepowerup.com: error'):
        return 'SitePowerUp error page; discussion was not recovered'
    return None


def belongs(page, url, board, forums=()):
    item = candidate(url, board)
    if not item or source_problem(page):
        return False
    declarations = getattr(page, 'board_fields', set())
    replies = getattr(page, 'reply_fields', set())
    if declarations and declarations != {board}:
        return False
    if item['kind'] == 'message' and replies and replies != {item['reply']}:
        return False
    # Indexes may link to other boards before their own navigation. Require an
    # own-board link or the platform reply form's hidden identity, not the first link.
    return declarations == {board} or any(candidate(link['url'], board) for link in page.links)


def forum_links(page, url, board):
    return []  # This platform has boards and messages, without Ezboard-style forums.


def catalog_url(board, query):
    action = {'display': 'Action=Display&', 'reply': 'Action=Reply&', 'default': ''}[query['kind']]
    return 'http://' + query['host'] + '/mb/view.asp?' + action + 'BoardID=' + board


def catalog_member(item, query):
    expected = 'reply' if query['kind'] == 'reply' else 'display'
    return item['action'] == expected


def archived_board(path):
    """Identify old downloader filenames without treating them as exact URLs."""
    if path.lower().startswith('mb/view.asp_'):
        path = path[:11] + '?' + path[12:].replace('_and_', '&')
    item = address('http://www.sitepowerup.com/' + path)
    return item['board'] if item and item['kind'] in ('board', 'message') else None


def sample_listing(downloader, url, start, end):
    """Prefer readable indexes, then messages; source URL spellings stay exact."""
    board = board_name(url)
    seen, limited = 0, False
    for kind in CATALOG_KINDS:
        query = {'host': 'www.sitepowerup.com', 'kind': kind}
        listing = downloader.call({'op': 'sitepowerup_list', 'url': catalog_url(board, query),
                                   'from': start, 'to': end, 'resume_key': None})
        seen += len(listing['captures'])
        limited = limited or bool(listing['resume_key'])
        records = [record for record in listing['captures']
                   if (item := candidate(record['url'], board)) and catalog_member(item, query)]
        if records:
            return {'captures': records, 'listing_limited': limited, 'available_rows': seen, 'identity_variants': []}
    return {'captures': [], 'listing_limited': limited, 'available_rows': seen, 'identity_variants': []}
