"""Source-structured regressions distilled from the November capture audit."""
import pytest
from indexer import passages

READER='Complete reader remains unchanged, including all source navigation.\r\n'

def project(raw, host='example.org', path='index.html'):
    result=passages.prepare(READER,f'websites/{host}/20000101000000/{path}',
                            html_source=raw,mime_type='text/html')
    passages.verify(READER,result)
    assert result.source_sha256==passages.digest(READER)
    return result

@pytest.mark.parametrize('name,heading',[
    ('Snow Orc Mountaineer','<p><font size="3"><b>{name}</b></font><br><a href="../Subscribe.asp?DID=7">[Subscribe to this document]</a></p>'),
    ('Resist Cold','<td><font size="5">{name}</font><a href="../Subscribe.asp?DID=7">[Subscribe to this document]</a></td>'),
    ('Giant, Sand','<font size="4">{name}<br><a href="../Subscribe.asp?DID=7"><font size="2">[Subscribe to this document]</font></a></font>')])
def test_everlore_source_entity_titles_every_reply(name,heading):
    raw='<html><title>Everlore - EverQuest</title>'+heading.format(name=name)+'''<table>
    <tr><td><b>Submitted by: Arin</b> On: 2/1/2001</td></tr>
    <tr><td colspan="2">These beauties are hard to find.</td></tr></table></html>'''
    result=project(raw,'www.everlore.com','bestiary/beastdetails.asp?BID=7')
    assert result.title==name
    assert all(p.title==name for p in result.passages)
    assert 'These beauties' in result.view


def test_allakhazam_class_label_is_entity_not_an_arbitrary_comment_heading():
    raw='''<html><title>Allakhazam's Magical Realm</title><table><tr>
    <td align="center" colspan="2"><b>The Cleric</b></td></tr></table>
    <h3>Class Strategy Discussions</h3><p>Healing advice from the community.</p></html>'''
    result=project(raw,'everquest.allakhazam.com','db/classes.html?class=7')
    assert result.title=='The Cleric'
    assert all('The Cleric' in p.request for p in result.passages)
    other=project(raw.replace('Class Strategy Discussions','Other Discussions'),
                  'everquest.allakhazam.com','db/classes.html?class=7')
    assert other.title!="The Cleric"


def menu():
    labels=['Current News','News Archive','Poll Archive','Submit News','Image Gallery','Contact Us']
    return '<table>'+''.join('<tr><td class="menuItem"><a href="/nav">'+s+'</a></td></tr>' for s in labels)+'</table>'


def test_generic_menu_filter_preserves_data_directories_notes_and_literal_examples():
    raw='<html><body>'+menu()+'''<h1>Guild reports</h1><p>The clerics defeated the dragon.</p>
    <aside>Meet by the waterfall on Tuesday.</aside>
    <table><caption>Raid plans</caption><tr><td><a href="/news">Current News</a></td><td>12</td></tr></table>
    <table><tr><td><a href="/guide">Healing guide</a></td><td>Explains resurrection and mana management.</td></tr></table>
    <blockquote>'''+menu()+'''</blockquote><pre>&lt;table class="menu"&gt;sample&lt;/table&gt;</pre></body></html>'''
    result=project(raw)
    assert result.view.count('Submit News')==1  # The quotation survives.
    for value in ['waterfall','Raid plans','12','mana management','sample','defeated the dragon']:
        assert value in result.view
    assert 'structured_navigation' in result.exclusions


def test_ad_directory_requires_source_signals_and_retains_authored_directories():
    links=''.join(f'<a href="?term=Game{i}">Game {i}</a> ' for i in range(40))
    ad='<html><title>Games</title><script>function popup(){window.open("/exitpage/popup.php");}</script>'+links+'</html>'
    assert project(ad).passages==()
    authored=project(ad.replace('</html>','<p>A hand maintained directory describing these games and their histories.</p></html>'))
    assert 'hand maintained' in authored.view and authored.passages
    assert project(ad.replace('window.open','example')).passages


def test_empty_vbulletin_calendar_and_frames_shells_are_not_semantic_content():
    calendar='''<html><title>Guild Forums - Calendar</title><body>Powered by vBulletin 3.5.0
    <form><input name="month"><input name="year"></form><table>'''+''.join(
        '<tr>'+''.join(f'<td><a href="calendar.php?day={n}">{n}</a></td>' for n in range(a,a+7))+'</tr>' for a in (1,8,15,22))+'''</table></body></html>'''
    result=project(calendar,path='forums/calendar.php')
    assert not result.passages and 'forum_utility_page' in result.exclusions
    event=project(calendar.replace('</td>','<a href="calendar.php?do=getinfo&e=13">Dragon raid at dusk</a></td>',1),path='forums/calendar.php')
    assert 'Dragon raid at dusk' in event.view
    shell='<html><frameset><frame src="main.html"></frameset><noframes><body>This page requires frames.</body></noframes></html>'
    assert not project(shell).passages
    assert 'Authored outside note' in project(shell.replace('</html>','<p>Authored outside note about the guild.</p></html>')).view
    assert 'Our guild meets every Friday' in project(shell.replace('This page requires frames.','Our guild meets every Friday by the bank.')).view


@pytest.mark.parametrize('skin',['wordpress','html5'])
def test_articles_have_separate_subject_date_context_and_preserve_outside_content(skin):
    def article(subject,date,body):
        if skin=='wordpress':
            return f'<div class="post-top"><div class="post-bottom"><div class="entry"><div class="title"><h2><a rel="bookmark">{subject}</a></h2><small>Posted by Arin on {date}</small></div><p>{body}</p></div></div></div>'
        return f'<article><h2>{subject}</h2><time datetime="{date}">{date}</time><p>{body}</p></article>'
    raw='<html><title>Guild news</title><p>Bring cold resistance on Friday.</p>'+article('Dragon defeated','October 12, 2006','Our clerics won the battle.')+article('Recruiting bards','October 1, 2006','Contact the guild leader.')+'<aside>Unique travel instructions.</aside></html>'
    result=project(raw)
    first=[p for p in result.passages if 'Our clerics won' in p.text]
    second=[p for p in result.passages if 'Contact the guild leader' in p.text]
    assert first and second
    assert all('Recruiting bards' not in p.text for p in first)
    assert all('Dragon defeated' in p.context and 'October 12' in p.context for p in first)
    assert all('Recruiting bards' in p.context and 'October 1' in p.context for p in second)
    assert 'cold resistance' in result.view and 'Unique travel instructions' in result.view


def test_ambiguous_article_layout_retains_content_without_invented_post_roles():
    result=project('<html><div class="entry"><h2>A note</h2><p>Unique content with no byline or date.</p></div></html>')
    assert 'Unique content' in result.view
    assert not any(p.kind=='post' for p in result.passages)


def test_html_newline_canonicalization_agrees_across_import_and_reindex():
    raw='<html>\n<title>Calendar notes</title>\n<table>\n<tr><td>A</td><td>B</td></tr>\n</table>\n<p>Authored note.</p>\n</html>'
    lf=project(raw)
    for newline in ('\r\n','\r'):
        result=project(raw.replace('\n',newline))
        assert (result.view,result.html_sha256,result.passages)==(lf.view,lf.html_sha256,lf.passages)


def test_calendar_plain_day_cells_keep_inline_authored_events():
    cells=''.join(f'<td>{n} Mon</td>' for n in range(1,29))
    raw='<html><title>Calendar</title><body>Powered by vBulletin<table><tr>'+cells+'</tr></table></body></html>'
    assert not project(raw,path='calendar.php').passages
    event=project(raw.replace('7 Mon','7 Mon Dragon raid at dusk'),path='calendar.php')
    assert 'Dragon raid at dusk' in event.view


def test_article_iso_dates_nested_skins_and_authored_tables_quotes_and_code():
    raw='''<html><title>News</title><div class="post"><div class="entry"><h2>Cleric raid</h2>
    <time>2006-10-12</time><p>The full report.</p><blockquote>Preserve this quotation.</blockquote>
    <table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Heal</td><td>400</td></tr></table>
    <pre>def heal():\n    return 400</pre></div></div>
    <article><h2>Next event</h2><time>2006-10-13</time><p>Another story.</p></article></html>'''
    result=project(raw)
    assert result.view.count('Article: Cleric raid')==1
    for needle in ['quotation','Heal','return 400']:
        found=[p for p in result.passages if needle in p.text]
        assert found and all('Cleric raid' in p.context for p in found)
    assert not any('Another story' in p.text and 'The full report' in p.text for p in result.passages)


def test_navigation_roles_with_authored_descriptions_are_retained():
    labels=['Current News','News Archive','Submit News','Contact Us','Search','Home']
    raw='<html><title>Research directory</title><table>'+''.join(
        f'<tr><td><a href="/guide">{label}</a></td><td>A carefully written description of the historical resources in this category.</td></tr>' for label in labels)+'</table></html>'
    assert project(raw).view.count('carefully written description')==6


def test_legacy_menu_roles_generalize_beyond_news_labels():
    raw='<html><body><table><tr><td class="menuSection">Game information</td></tr>'+''.join(
        f'<tr><td><a class="menuItem" href="/menu">{label}</a></td></tr>' for label in
        ['Spell list','Zone atlas','Crafting','Bestiary','Maps','Guilds'])+'</table><h1>Raid report</h1><p>We defeated the dragon at dusk.</p></body></html>'
    result=project(raw)
    assert 'Zone atlas' not in result.view and 'defeated the dragon' in result.view
