"""Source-verified forum discussion identities; exact capture identity is separate.

No title-based grouping, network requests, model calls or archive walks. Lithium
RSS links prove reply membership; its print/HTML views require that evidence.
Other supported engines require both a structured page and an explicit topic ID.
"""
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
import sqlite3
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from .sqlite_state import SQLiteState
from . import passage_forums

VERSION = 'forum-discussions-v1'
MAPPING = {'type': 'object', 'dynamic': 'strict', 'properties': {'version': {'type': 'keyword'}, 'key': {'type': 'keyword'}, 'platform': {'type': 'keyword'}, 'origin': {'type': 'keyword'}, 'forum': {'type': 'keyword'}, 'topic': {'type': 'keyword'}, 'title': {'type': 'text', 'index': False}, 'source_sha256': {'type': 'keyword', 'index': False, 'doc_values': False}, 'evidence_sha256': {'type': 'keyword', 'index': False, 'doc_values': False}, 'evidence_id': {'type': 'keyword', 'index': False, 'doc_values': False}}}
MAX_BYTES = 2 * 1024 * 1024
LOG = logging.getLogger(__name__)
NUMBER = re.compile(r'[1-9][0-9]{0,19}')
REPLY = re.compile(r'^(?:re\s*:\s*)+', re.I)


def original_url(record_id, replay=None):
    match = re.fullmatch(r'websites/([^/]+)/([0-9]{14})/(.*)', record_id)
    if not match:
        return None
    fallback = 'http://' + match[1] + '/' + match[3]
    if replay:
        archived = re.fullmatch(r'https?://web\.archive\.org/web/([0-9]{14})(?:[a-z]+_)?/(https?://.+)', replay, re.I)
        if not archived or archived[1] != match[2]:
            return None
        fallback = archived[2]
    try:
        url = urlsplit(fallback)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
            return None
        # A proof cannot be borrowed from a different stored host/path.
        if url.netloc.lower() != match[1].lower() or url.path + ('?' + url.query if url.query else '') != '/' + match[3]:
            return None
        return urlunsplit((url.scheme, url.netloc.lower(), url.path, url.query, ''))
    except ValueError:
        return None


def parameters(url):
    pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    if len({key for key, _ in pairs}) != len(pairs):
        return None
    return dict(pairs)


def lithium_message(url):
    try:
        parsed = urlsplit(url)
        match = re.fullmatch(r'(/[^?#]*)/board/(message|print)', parsed.path)
        args = parameters(url)
        if (not match or args is None or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', args.get('board.id', ''))
                or not NUMBER.fullmatch(args.get('message.id', '')) or parsed.username or parsed.password):
            return None
        origin = urlunsplit((parsed.scheme, parsed.netloc.lower(), '', '', ''))
        return origin, match[1], args['board.id'], args['message.id']
    except ValueError:
        return None


def member_key(parts):
    return json.dumps(parts, separators=(',', ':'))


def topic_link(soup, url, topic, actions, parameter='t'):
    original=urlsplit(url)
    for link in soup.find_all(['a','link'],href=True):
        if link.find_parent(['blockquote','pre','code']) or link.find_parent(class_='postbody'):
            continue
        target=urlsplit(urljoin(url,link['href']));args=parameters(target.geturl())
        if (target.scheme==original.scheme and target.netloc==original.netloc
                and target.path.rsplit('/',1)[0]==original.path.rsplit('/',1)[0]
                and target.path.rsplit('/',1)[-1] in actions and args and args.get(parameter)==topic):
            return True
    return False


def proof(platform, origin, forum, topic, title, record_id, raw):
    identity = json.dumps([platform, origin, forum, topic], separators=(',', ':'))
    return {'version': VERSION, 'key': hashlib.sha256(identity.encode()).hexdigest(),
            'platform': platform, 'origin': origin, 'forum': forum, 'topic': topic,
            'title': ' '.join(title.split())[:300], 'evidence_id': record_id,
            'evidence_sha256': hashlib.sha256(raw).hexdigest()}


@dataclass(frozen=True)
class FeedEvidence:
    discussion: dict
    members: tuple


def lithium_feed(record_id, raw, replay=None):
    url = original_url(record_id, replay)
    if not url or len(raw) > MAX_BYTES or re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', raw, re.I):
        return None
    parsed = urlsplit(url); args = parameters(url)
    start = raw[3:] if raw.startswith(b'\xef\xbb\xbf') else raw
    if not start.lstrip().startswith((b'<?xml', b'<rss')):
        return None
    if parsed.path != '/rss/message' or args is None:
        return None
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        return None
    channels = root.findall('channel')
    if root.tag != 'rss' or len(channels) != 1:
        return None
    channel = channels[0]
    selected = lithium_message(channel.findtext('link') or '')
    items = channel.findall('item')
    if not selected or not items or len(items) > 500:
        return None
    origin, community, board, target = selected
    if (origin != urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
            or args.get('board.id') != board or args.get('message.id') != target):
        return None
    first = lithium_message(items[0].findtext('link') or '')
    title = (items[0].findtext('title') or '').strip()
    if not first or not title or REPLY.match(title) or first[:3] != selected[:3]:
        return None
    if REPLY.sub('', channel.findtext('title') or '').strip() != title:
        return None
    members = [selected]
    numbers = set()
    for item in items:
        link = item.findtext('link') or ''
        member = lithium_message(link)
        if (not member or member[:3] != selected[:3] or member[3] in numbers
                or urlsplit(link).fragment != 'M' + member[3] or int(member[3]) < int(first[3])):
            return None
        numbers.add(member[3]); members.append(member)
    value = proof('lithium', origin, community + '/' + board, first[3], title, record_id, raw)
    return FeedEvidence(value, tuple(dict.fromkeys(members)))


class Catalog:
    """Private evidence index. Conflicting source memberships stay ambiguous."""
    def __init__(self, path):
        self.state = SQLiteState(path, durable=True)
        with self.state.transaction(attempts=6) as db:
            db.execute('CREATE TABLE IF NOT EXISTS members (member TEXT PRIMARY KEY, discussion TEXT NOT NULL, ambiguous INTEGER NOT NULL DEFAULT 0)')

    def learn(self, evidence):
        if not evidence:
            return
        encoded = json.dumps(evidence.discussion, sort_keys=True)
        with self.state.transaction(attempts=6) as db:
            for member in evidence.members:
                key = member_key(member)
                previous = db.execute('SELECT discussion FROM members WHERE member=?', (key,)).fetchone()
                if previous and json.loads(previous[0])['key'] != evidence.discussion['key']:
                    db.execute('UPDATE members SET ambiguous=1 WHERE member=?', (key,))
                elif previous is None:
                    db.execute('INSERT INTO members(member,discussion) VALUES (?,?)', (key, encoded))

    def learn_safe(self, evidence):
        try:
            self.learn(evidence)
        except (sqlite3.Error, OSError) as error:
            LOG.warning('Discussion evidence could not be saved (%s); uncertain pages remain separate', type(error).__name__)

    def lookup(self, member):
        if not member:
            return None
        row = self.state.connection().execute('SELECT discussion FROM members WHERE member=? AND ambiguous=0', (member_key(member),)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.state.close()


def optional_catalog():
    path = os.environ.get('DISCUSSION_CATALOG_PATH')
    if not path:
        return None
    try:
        return Catalog(path)
    except (OSError, ValueError, sqlite3.Error) as error:
        LOG.warning('Discussion evidence catalog unavailable (%s); uncertain pages remain separate', type(error).__name__)
        return None


def _metadata(record_id, raw, replay=None, *, catalog=None):
    """Return a complete proof or None; partial/unknown layouts stay separate."""
    url = original_url(record_id, replay)
    if not url or len(raw) > MAX_BYTES:
        return None
    feed = lithium_feed(record_id, raw, replay)
    if feed:
        if catalog:
            catalog.learn(feed)
            value = catalog.lookup(lithium_message(ET.fromstring(raw).find('channel').findtext('link')))
        else:
            value = feed.discussion
    else:
        parsed = urlsplit(url); args = parameters(url)
        if args is None:
            return None
        # Only parse plausible forum routes; ordinary pages never borrow a title.
        lithium = lithium_message(url)
        topic = args.get('t', '')
        phpbb = parsed.path.endswith('/viewtopic.php') and NUMBER.fullmatch(topic)
        vbulletin = parsed.path.endswith('/showthread.php') and NUMBER.fullmatch(topic)
        snitz = parsed.path.endswith(('/topic.asp','/pop_printer_friendly.asp')) and NUMBER.fullmatch(args.get('TOPIC_ID',''))
        ezboard = re.fullmatch(r'(.+)\.showMessage(?:Range)?',parsed.path)
        ez_topic = re.fullmatch(r'([1-9][0-9]{0,19})\.topic',args.get('topicID',''))
        ezboard = ezboard if ez_topic and (parsed.hostname or '').endswith('.ezboard.com') else None
        if not lithium and not phpbb and not vbulletin and not snitz and not ezboard:
            return None
        soup = BeautifulSoup(raw, 'html.parser')
        value = None
        if lithium and catalog and soup.select_one('.msg_text_cell') and soup.select_one('.msg_user_cell') and soup.select_one('.msg_date_cell'):
            # The page must itself link the requested message, outside quotes.
            linked = any(lithium_message(urljoin(url, a.get('href', ''))) == lithium
                         and not a.find_parent(['blockquote', 'pre', 'code']) for a in soup.find_all('a', href=True))
            if linked:
                value = catalog.lookup(lithium)
        elif phpbb and soup.select_one('.postbody') and (soup.select_one('.postdetails') or soup.select_one('a[name^="p"]')) and topic_link(soup,url,topic,{'viewtopic.php'}):
            value = proof('phpbb', urlunsplit((parsed.scheme, parsed.netloc, '', '', '')), parsed.path.rsplit('/', 1)[0] or '/', topic,
                          soup.title.get_text(' ', strip=True) if soup.title else '', record_id, raw)
        elif vbulletin and soup.select_one('[id^="post_message_"]') and soup.select_one('[id^="postcount"]') and topic_link(soup,url,topic,{'showthread.php','printthread.php'}):
            value = proof('vbulletin', urlunsplit((parsed.scheme, parsed.netloc, '', '', '')), parsed.path.rsplit('/', 1)[0] or '/', topic,
                          soup.title.get_text(' ', strip=True) if soup.title else '', record_id, raw)
        elif snitz and passage_forums.snitz(soup,str(soup)):
            linked=topic_link(soup,url,args['TOPIC_ID'],{'topic.asp','pop_printer_friendly.asp'},'TOPIC_ID')
            if linked:
                value=proof('snitz',urlunsplit((parsed.scheme,parsed.netloc,'','','')),parsed.path.rsplit('/',1)[0] or '/',args['TOPIC_ID'],
                            soup.title.get_text(' ',strip=True) if soup.title else '',record_id,raw)
        elif ezboard and passage_forums.ezboard(soup,parsed.hostname):
            actions={ezboard[1].rsplit('/',1)[-1]+suffix for suffix in ('.showMessage','.showMessageRange')}
            linked=topic_link(soup,url,args['topicID'],actions,'topicID')
            if linked:
                value=proof('ezboard',urlunsplit((parsed.scheme,parsed.netloc,'','','')),ezboard[1],ez_topic[1],
                            soup.title.get_text(' ',strip=True) if soup.title else '',record_id,raw)
    if not value:
        return None
    return {**value, 'source_sha256': hashlib.sha256(raw).hexdigest()}


def metadata(record_id, raw, replay=None, *, catalog=None, strict=False):
    if strict:
        return _metadata(record_id, raw, replay, catalog=catalog)
    try:
        return _metadata(record_id, raw, replay, catalog=catalog)
    except (sqlite3.Error, OSError, ValueError, RecursionError) as error:
        LOG.warning('Discussion evidence unavailable (%s); uncertain pages remain separate', type(error).__name__)
        return _metadata(record_id, raw, replay, catalog=None)
