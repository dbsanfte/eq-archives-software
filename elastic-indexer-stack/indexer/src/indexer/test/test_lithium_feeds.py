"""Regression layouts distilled from official-forum captures; no archive fixtures."""
from html import escape
import pytest
from indexer import passages

READER='Complete unchanged archival reader, including headers and controls.'
RID='websites/unrelated.example/20061201000000/eq/board/print?board.id=Clerics&message.id=1'

def plan(raw,rid=RID,mime='text/html'):
    result=passages.prepare(READER,rid,html_source=raw,mime_type=mime)
    passages.verify(READER,result)
    return result


def printed(author,body='Unique raid testimony.',date='12-11-2006 09:29 AM',subject='Cleric raids'):
    return f'''<table border="1"><tr><td>&nbsp;</td><td>{subject}</td></tr><tr>
    <td class="msg_user_cell" width="200">{author}<img alt="" src="holiday.gif"><br>Journeyman<br>Posts: 1000</td>
    <td class="msg_text_cell" rowspan="2">{body}</td></tr>
    <tr><td class="msg_date_cell"><p>{date}</p></td></tr></table>'''


@pytest.mark.parametrize('author',['Guest Cleric','<span style="color:#336600">Guest Cleric</span>'])
def test_lithium_print_plain_and_coloured_authors_keep_attribution_and_remove_rank(author):
    raw='<html><title>Forum print</title><nav>Global controls</nav>'+printed(author)+printed('Second Cleric','Second reply.',subject='Re: Cleric raids')+'</html>'
    result=plan(raw)
    assert result.method=='forum_posts'
    assert len(result.passages)==2
    assert 'Post by Guest Cleric' in result.passages[0].context
    assert '12-11-2006' in result.passages[0].context
    assert 'Second Cleric' in result.passages[1].context
    assert 'Second reply' not in result.passages[0].text
    assert all(value not in result.view for value in ('Journeyman','Posts: 1000','Global controls'))


def test_lithium_print_quotes_tables_attachments_and_damaged_headers_keep_content():
    body='<p>Opening testimony.</p><blockquote>Quoted raid advice.</blockquote><table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Heal</td><td>400</td></tr></table><a href="notes.txt">raid-notes.txt</a>'
    for raw in [printed('Guest',body),printed('Guest',body).replace('class="msg_date_cell"','class="damaged"')]:
        result=plan(raw)
        assert all(value in result.view for value in ('Opening testimony','Quoted raid advice','Heal','400','raid-notes.txt'))
    result=plan(printed('Guest',body))
    assert any(p.kind=='quote' for p in result.passages)
    assert all('Guest' in p.context for p in result.passages)


def rss(items):
    return '<rss xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>Guild board</title>'+''.join(items)+'</channel></rss>'


def item(subject,body,author='Arin',date='Mon, 16 Oct 2006 14:45:26 GMT'):
    return f'<item><title>{escape(subject)}</title><description><![CDATA[{body}]]></description><pubDate>{date}</pubDate><author>{author}</author></item>'


def feed(raw):return plan(raw,'websites/unrelated.example/20061201000000/rss/board?board.id=Guild',mime='text/xml')


def test_feed_each_long_post_keeps_subject_author_and_date_across_body_headings():
    raw=rss([item('Cleric raids','<h3>Spell choices</h3><p>'+('Healing testimony alpha. '*210)+'</p>'),
             item('Bard recruitment','<p>'+('Recruitment testimony beta. '*210)+'</p>',author='Bryn',date='Tue, 17 Oct 2006 12:30:00 GMT')])
    result=feed(raw)
    assert result.method=='feed_entries'
    for needle,author,subject,date in [('alpha','Arin','Cleric raids','16 Oct 2006'),('beta','Bryn','Bard recruitment','17 Oct 2006')]:
        found=[p for p in result.passages if needle in p.text]
        assert len(found)>1
        assert all(author in p.context and subject in p.context and date in p.context for p in found)
    assert not any('alpha' in p.text and 'beta' in p.text for p in result.passages)


def test_decorative_tildes_and_backticks_cannot_consume_the_next_feed_entry():
    raw=rss([item('February update','<p>~~~~~~~~~~~~~~~~~~~~~~~~~~~~</p><p>February change alpha.</p>'),
             item('December update','<p>December change beta.</p>',author='Bryn'),
             item('October update','<p>```</p><p>October change gamma.</p>',author='Cora')])
    result=feed(raw)
    assert not any(p.kind=='code' for p in result.passages)
    for marker,name in [('alpha','Arin'),('beta','Bryn'),('gamma','Cora')]:
        found=[p for p in result.passages if marker in p.text]
        assert found and all(name in p.context for p in found)
        assert all(sum(token in p.text for token in ('alpha','beta','gamma'))==1 for p in found)


def test_feed_actual_code_quotes_and_data_tables_retain_their_own_post_context():
    raw=rss([item('Cleric macros','<blockquote>Quoted healing claim.</blockquote><pre><code>def heal():\n    return 400</code></pre><table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Heal</td><td>400</td></tr></table>'),item('Other news','<p>Separate story.</p>',author='Bryn')])
    result=feed(raw)
    assert any(p.kind=='code' and 'return 400' in p.text for p in result.passages)
    assert any(p.kind=='quote' and 'Quoted healing' in p.text for p in result.passages)
    assert any(p.kind=='table' and 'Heal' in p.text for p in result.passages)
    assert all('Arin' in p.context and 'Cleric macros' in p.context for p in result.passages if any(v in p.text for v in ('400','Quoted healing')))


def test_atom_xhtml_and_plain_text_authors_dates_and_literal_markup():
    raw='''<feed xmlns="http://www.w3.org/2005/Atom"><title>Guild news</title>
    <entry><title>Raid notes</title><author><name>Arin</name></author><published>2006-10-16T14:45:26Z</published>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Full raid testimony.</p><blockquote>Preserved claim.</blockquote></div></content></entry>
    <entry><title>Markup guide</title><author><name>Bryn</name></author><updated>2006-10-17T12:00:00Z</updated>
    <content type="text">Use &lt;script&gt; as a literal example.</content></entry></feed>'''
    result=feed(raw)
    assert result.method=='feed_entries'
    assert 'Full raid testimony' in result.view and 'literal example' in result.view
    assert any('Arin' in p.context and '2006-10-16' in p.context for p in result.passages)
    assert any('Bryn' in p.context and 'Markup guide' in p.context for p in result.passages)


def test_feed_content_encoded_and_dc_creator_preserve_unique_summary_and_full_body():
    raw=rss(['''<item><title>Cleric report</title><description>Unique summary note.</description>
    <content:encoded><![CDATA[<p>Full detailed report.</p>]]></content:encoded>
    <dc:creator>Arin</dc:creator><dc:date>2006-10-16T14:45:26Z</dc:date></item>'''])
    result=feed(raw)
    assert 'Unique summary note' in result.view and 'Full detailed report' in result.view
    assert all('Arin' in p.context and '2006-10-16' in p.context for p in result.passages)


def test_authorless_feed_retains_subject_without_inventing_author():
    result=feed(rss([item('Cleric report','<p>Unattributed testimony.</p>',author='')]))
    assert 'Unattributed testimony' in result.view
    assert all('Cleric report' in p.context for p in result.passages)
    assert 'Post by' not in result.view


def test_feed_scripts_removed_but_literal_code_and_authored_links_retained():
    result=feed(rss([item('Cleric report','<script>alert("ad-code")</script><p>Authored testimony <a href="guide.html">spell guide</a>.</p><pre>&lt;script&gt;literal example&lt;/script&gt;</pre>')]))
    assert 'ad-code' not in result.view
    assert 'literal example' in result.view and 'spell guide' in result.view


def test_malformed_or_entity_declaring_feed_keeps_content_without_xml_expansion():
    for raw in [rss([item('Broken feed','<p>Retained body.</p>')]).replace('</item>',''),
                '<!DOCTYPE rss [<!ENTITY injected "invented expansion">]>'+rss([item('Guarded feed','<p>Retained body.</p>')])]:
        result=feed(raw)
        assert 'Retained body' in result.view and 'invented expansion' not in result.view


def notify(body=''):
    return f'''<html><title>Notify Moderator - Forums</title><h1>Notify Moderator</h1>
    <form action="/eq/notification" id="notify"><input type="hidden" name="board.id" value="Clerics">
    <input type="hidden" name="message.id" value="1"><input type="hidden" name="notification.type" value="1">
    <label>Additional Notes to Moderator</label><textarea name="notification.body">{body}</textarea>
    <input type="submit" value="Notify Moderator"></form></html>'''


def test_empty_lithium_notification_is_utility_but_authored_notes_and_examples_remain():
    rid='websites/unrelated.example/20061201000000/eq/notification?board.id=Clerics&message.id=1'
    result=plan(notify(),rid)
    assert result.passages==() and 'forum_utility_page' in result.exclusions
    assert 'Surviving note to the moderator' in plan(notify('Surviving note to the moderator.'),rid).view
    assert 'Authored explanation' in plan(notify().replace('<h1>','<article><p>Authored explanation of moderator reporting.</p><h1>').replace('</html>','</article></html>'),rid).view
    # URL/title alone are insufficient.
    assert plan('<html><title>Notify Moderator</title><p>Authored reporting guide.</p></html>',rid).passages


def test_literal_underscores_in_source_author_names_survive_context_shortening():
    for result in (plan(printed('Guest_Cleric')),feed(rss([item('Cleric report','<p>Healing testimony.</p>',author='Guest_Cleric')]))):
        assert all('Guest_Cleric' in p.context for p in result.passages)
        assert not any('Guest\\ ' in p.context for p in result.passages)
