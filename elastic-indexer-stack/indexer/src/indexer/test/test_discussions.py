import hashlib
from pathlib import Path

import pytest

from indexer.discussions import Catalog, MAX_BYTES, VERSION, lithium_feed, metadata, original_url, optional_catalog

HOST = 'forums.example.org'
ROOT = 'http://' + HOST
STAMP = '20061213163803'


def key(path, host=HOST):
    return 'websites/' + host + '/' + STAMP + '/' + path


def feed(target='20', board='Veterans', root='10', title='A zone discussion', origin=ROOT):
    return (f'<rss version="2.0"><channel><title>Re: {title}</title>'
            f'<link>{origin}/eq/board/message?board.id={board}&amp;message.id={target}</link>'
            f'<description>Selected reply body</description>'
            f'<item><title>{title}</title><link>{origin}/eq/board/message?board.id={board}&amp;message.id={root}#M{root}</link>'
            '<description>Original thread body</description></item>'
            f'<item><title>Re: {title}</title><link>{origin}/eq/board/message?board.id={board}&amp;message.id=15#M15</link>'
            '<description>Another message</description></item></channel></rss>').encode()


def page(target='20', board='Veterans'):
    return (f'<html><a href="/eq/board/message?board.id={board}&amp;message.id={target}">Re: A zone discussion</a>'
            '<table><td class="msg_user_cell">Author</td><td class="msg_text_cell">A reply</td>'
            '<td class="msg_date_cell">12-12-2006 01:00 PM</td></table></html>').encode()


def test_rss_and_print_views_share_source_verified_discussion_without_merging_capture_urls(tmp_path):
    catalog = Catalog(tmp_path/'catalog.sqlite')
    path = key('rss/message?board.id=Veterans&message.id=20')
    first = metadata(path, feed(), catalog=catalog)
    second = metadata(key('rss/message?message.id=21&board.id=Veterans'), feed('21'), catalog=catalog)
    printed = metadata(key('eq/board/print?board.id=Veterans&message.id=20&page=1&format=all'), page(), catalog=catalog)
    assert first['key'] == second['key'] == printed['key']
    assert printed['topic'] == '10' and printed['title'] == 'A zone discussion'
    assert printed['version'] == VERSION and printed['source_sha256'] == hashlib.sha256(page()).hexdigest()
    assert printed['evidence_id'] == path and printed['evidence_sha256'] == hashlib.sha256(feed()).hexdigest()
    catalog.close()
    reopened = Catalog(tmp_path/'catalog.sqlite')
    assert metadata(key('eq/board/message?message.id=15&board.id=Veterans'), page('15'), catalog=reopened)['key'] == first['key']
    reopened.close()


def test_same_titles_in_other_boards_hosts_threads_and_protocols_remain_distinct():
    values = []
    for host, board, root, scheme in [(HOST,'Veterans','10','http'),(HOST,'Other','10','http'),
                                      (HOST,'Veterans','11','http'),('other.example.org','Veterans','10','http'),
                                      (HOST,'Veterans','10','https')]:
        url = scheme+'://'+host
        result = metadata(key('rss/message?board.id='+board+'&message.id=20',host), feed(board=board,root=root,origin=url),
                          'https://web.archive.org/web/'+STAMP+'/'+url+'/rss/message?board.id='+board+'&message.id=20')
        values.append(result['key'])
    assert len(set(values)) == len(values)


@pytest.mark.parametrize('raw',[
    b'not xml',b'<rss><channel>',b'<feed/>',b'<rss/>',
    feed().replace(b'<title>A zone discussion</title>', b'<title>Re: A zone discussion</title>'),
    feed().replace(b'#M10',b'#M99'),feed().replace(b'message.id=15#M15',b'message.id=9#M9'),
    feed().replace(b'<channel>',b'<channel><title>unrelated</title>'),
    feed().replace(b'board.id=Veterans&amp;message.id=15',b'board.id=Other&amp;message.id=15'),
    feed().replace(b'http://forums.example.org/eq/board/message?board.id=Veterans&amp;message.id=15',b'http://evil.example.org/eq/board/message?board.id=Veterans&amp;message.id=15'),
    b'<!DOCTYPE rss [<!ENTITY x "payload">]>'+feed(),b'x'*(MAX_BYTES+1),
])
def test_partial_spoofed_and_ambiguous_feeds_do_not_create_memberships(raw):
    assert metadata(key('rss/message?board.id=Veterans&message.id=20'),raw) is None


def test_selected_channel_must_match_exact_source_identity_and_unique_parameters():
    for path in ('rss/message?board.id=Other&message.id=20','rss/message?board.id=Veterans&message.id=99',
                 'rss/message?board.id=Veterans&message.id=20&message.id=21','ordinary/page'):
        assert metadata(key(path),feed()) is None
    assert metadata(key('rss/message?board.id=Veterans&message.id=20'),feed(origin='http://other.example.org')) is None
    assert metadata(key('rss/message?board.id=Veterans&message.id=20'),feed(),ROOT+'/rss/message?board.id=Veterans&message.id=20') is None


def test_unknown_print_pages_and_quoted_links_do_not_borrow_membership(tmp_path):
    path=key('eq/board/print?board.id=Veterans&message.id=20')
    assert metadata(path,page()) is None
    catalog=Catalog(tmp_path/'catalog.sqlite');catalog.learn(lithium_feed(key('rss/message?board.id=Veterans&message.id=20'),feed()))
    assert metadata(path,b'<h1>A zone discussion</h1>',catalog=catalog) is None
    assert metadata(path,page().replace(b'<a ',b'<blockquote><a ').replace(b'</a>',b'</a></blockquote>'),catalog=catalog) is None
    assert metadata(key('eq/board/print?board.id=Veterans&message.id=99'),page('99'),catalog=catalog) is None
    assert metadata(path,page().replace(b'msg_text_cell',b'ordinary'),catalog=catalog) is None
    catalog.close()


def test_conflicting_membership_is_retained_as_ambiguous_across_restarts(tmp_path):
    path=tmp_path/'catalog.sqlite';catalog=Catalog(path)
    a=lithium_feed(key('rss/message?board.id=Veterans&message.id=20'),feed())
    b=lithium_feed(key('rss/message?board.id=Veterans&message.id=20'),feed(root='11'))
    catalog.learn(a);catalog.learn(a);catalog.learn(None);catalog.learn(b);catalog.close()
    catalog=Catalog(path)
    assert metadata(key('eq/board/message?board.id=Veterans&message.id=20'),page(),catalog=catalog) is None
    assert metadata(key('rss/message?board.id=Veterans&message.id=20'),feed(),catalog=catalog) is None
    catalog.close()


@pytest.mark.parametrize('platform,path,signature',[
    ('phpbb','viewtopic.php?t=12&start=20','<div class="postbody">body</div><span class="postdetails">author</span>'),
    ('vbulletin','showthread.php?t=12&page=2','<div id="post_message_34">body</div><a id="postcount34">author</a>')])
def test_explicit_topic_ids_require_platform_evidence_and_preserve_other_topics(platform,path,signature):
    raw=('<html><title>Topic</title><a href="'+path.split('&')[0]+'">Topic</a>'+signature+'</html>').encode()
    a=metadata(key('forum/'+path),raw);b=metadata(key('forum/'+path.split('&')[0]),raw)
    assert a['key']==b['key'] and a['platform']==platform
    assert metadata(key('forum/'+path.replace('t=12','t=13')),raw.replace(b't=12',b't=13'))['key']!=a['key']
    assert metadata(key('other/'+path),raw)['key']!=a['key']
    assert metadata(key('forum/'+path),b'<html><title>Topic</title><p>ordinary</p></html>') is None
    assert metadata(key('forum/'+path.replace('t=12','t=12&t=13')),raw) is None


@pytest.mark.parametrize('record,url',[
    ('mail/example',None),(key('page'),'javascript:alert(1)'),
    (key('page'),'https://web.archive.org/web/'+STAMP+'/http://evil.example/page'),
    (key('page'),'https://web.archive.org/web/'+STAMP+'/http://user:pass@'+HOST+'/page'),
    (key('page'),'https://web.archive.org/web/'+STAMP+'/http://'+HOST+'/different'),
    (key('page'),'https://web.archive.org/web/20000101000000/http://'+HOST+'/page'),
    ('websites/[invalid/'+STAMP+'/page',None)])
def test_unverified_capture_identity_is_rejected(record,url):
    assert original_url(record,url) is None


def test_optional_catalog_storage_failure_keeps_unknown_pages_separate(tmp_path,monkeypatch,caplog):
    monkeypatch.delenv('DISCUSSION_CATALOG_PATH',raising=False)
    assert optional_catalog() is None
    monkeypatch.setenv('DISCUSSION_CATALOG_PATH','relative.sqlite')
    assert optional_catalog() is None and 'unavailable' in caplog.text
    monkeypatch.setenv('DISCUSSION_CATALOG_PATH',str(tmp_path/'state.sqlite'))
    catalog=optional_catalog();assert isinstance(catalog,Catalog);catalog.close()


def test_ezboard_thread_pagination_requires_structured_messages_and_matching_topic_links():
    path='fguildfrm1.showMessage?topicID=12.topic&start=20'
    raw=b'''<html><title>A guild topic</title><a href="fguildfrm1.showMessage?topicID=12.topic">Topic</a>
    <table><tr><td>Author</td><td>Comment</td></tr><tr><td><a href="bguild.showUserPublicProfile?gid=healer">Healer</a>
    <span class="title">Healer</span><br>12/12/06 1:02:03 pm</td><td>We fought the dragon.</td></tr></table></html>'''
    result=metadata(key(path,'pub5.ezboard.com'),raw)
    assert result['platform']=='ezboard' and result['topic']=='12'
    assert metadata(key(path.replace('&start=20','&start=40'),'pub5.ezboard.com'),raw)['key']==result['key']
    assert metadata(key(path,'ordinary.example.org'),raw) is None
    assert metadata(key(path,'pub5.ezboard.com'),b'<title>A guild topic</title>') is None
    assert metadata(key(path,'pub5.ezboard.com'),raw.replace(b'fguildfrm1.showMessage?topicID=12.topic',b'fguildfrm1.showMessageOther?topicID=12.topic')) is None


def test_snitz_topics_require_native_topic_links_and_structural_message_evidence():
    raw=b'''<html><title>Healing advice</title><p>Snitz Forums</p><a href="topic.asp?TOPIC_ID=12">Topic</a>
    <table><tr><td><a href="pop_profile.asp?id=1">Healer</a></td><td>Posted - 01/02/2000 : 03:04:05 PM<hr>
    <span class="spnMessageText" id="msg">Bring healing potions.</span></td></tr></table></html>'''
    first=metadata(key('forum/topic.asp?TOPIC_ID=12'),raw)
    assert first['platform']=='snitz' and first['topic']=='12'
    assert metadata(key('forum/topic.asp?TOPIC_ID=12&whichpage=2'),raw)['key']==first['key']
    assert metadata(key('forum/topic.asp?TOPIC_ID=13'),raw) is None
    assert metadata(key('forum/topic.asp?TOPIC_ID=12'),b'<title>Healing advice</title>') is None
    assert metadata(key('forum/topic.asp?TOPIC_ID=12'),raw.replace(b'topic.asp?TOPIC_ID=12',b'ordinary.asp?TOPIC_ID=12')) is None
