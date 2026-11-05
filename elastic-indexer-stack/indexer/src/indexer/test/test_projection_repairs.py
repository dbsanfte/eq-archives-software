"""Source-derived projections for platform skins and broken inline scripts."""
import html
import pytest
from indexer import passages
from indexer.html_extraction import repair_script_markup


def plan(raw,host='unrelated.example',path='article.html'):
    result=passages.prepare('Complete reader with original controls and sources',
        f'websites/{host}/20000101000000/{path}',html_source=raw,mime_type='text/html')
    passages.verify('Complete reader with original controls and sources',result)
    return result


def comment(body,subject='Question about the raid'):
    return f'''<table class=mform><form><tr><td id=headerrow colspan=2>{subject}<small>Posted @ Jan 1 2000</small></td></tr>
    <tr><td><span>By:</span><a href="poster.html?user=1">Healer</a>900 posts Score: Good</td><td class=mform>{body}</td></tr>
    <tr><td colspan=2 align=right>0 Replies</td></tr></form></table>'''


@pytest.mark.parametrize('body',['','&nbsp; '*8,'<p>&nbsp;</p><br>'])
def test_allakhazam_empty_body_keeps_subject_without_fabricating_profile_text(body):
    p=plan('<h1>Dragon Scale</h1>'+comment(body),'everquest.allakhazam.com')
    assert 'Question about the raid' in p.view and 'Dragon Scale'==p.title
    assert 'empty_allakhazam_comment_body' in p.exclusions
    assert all(t not in p.view for t in ('900 posts','Score:','0 Replies','By:'))
    assert 'Healer' in p.view


def test_allakhazam_empty_subject_and_body_do_not_create_metadata_only_passages():
    p=plan('<h1>Dragon Scale</h1>'+comment('&nbsp;',subject=''),'eqbeastiary.allakhazam.com')
    assert 'Healer' not in p.view and 'Score:' not in p.view
    assert not any(c.kind=='post' for c in p.passages)


@pytest.mark.parametrize('body',['<img src="raid.png" alt="Raid screenshot">','<a href="notes.txt">Download testimony</a>',
    '0 Replies','<blockquote>Real quotation.</blockquote><table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Heal</td><td>400</td></tr></table>'])
def test_allakhazam_real_content_and_attachment_only_messages_are_retained(body):
    p=plan(comment(body),'eqdb.allakhazam.com')
    assert 'empty_allakhazam_comment_body' not in p.exclusions
    for value in ('Raid screenshot','Download testimony','0 Replies','Real quotation.','400'):
        if value in body:assert value in p.view
    assert '900 posts' not in p.view


def ezboard(labels=('Auteur','Commentaire'),guest=False,missing_date=False,nested=False):
    rows=''
    for i in range(2):
        profile=f'<a href="ucleric{i}.showPublicProfile?language=FR">Cleric{i}</a>' if not guest or i==0 else f'Cleric{i}'
        date='' if missing_date and i==0 else f'(12/{i+1}/01 8:45:01 am)'
        body=f'<p>Distinct testimony {i}.</p><blockquote>Real quote {i}.</blockquote><a href="raid-{i}.txt">Attachment {i}</a>'
        if nested:body+='<table><tr><td>Author</td><td>Comment</td></tr><tr><td>Quoted Author</td><td>Genuine nested data.</td></tr></table>'
        rows+=f'<tr><td><span class=title>{profile}</span><span class=usertitle>Visiteur</span>{date}<a href="fclerics.showAddReplyScreenFromWeb?topicID=1&index={i}">Répondre</a></td><td class=m>{body}</td></tr>'
    return '<title>Cleric raid</title><p>Shared site navigation</p><table><tr>'+''.join('<td class=headings>'+x+'</td>' for x in labels)+'</tr>'+rows+'</table><p>Upgrade your account</p>'


@pytest.mark.parametrize('labels',[('Auteur','Commentaire'),('Autor','Beitrag'),('作者','投稿')])
@pytest.mark.parametrize('host',['pub37.ezboard.com','server9.ezboard.com'])
def test_localized_ezboard_recognizes_structural_posts_without_language_or_host_exceptions(labels,host):
    p=plan(ezboard(labels),host,'fclerics.showMessage?topicID=1')
    assert 'ezboard_posts' in p.exclusions
    assert 'Shared site navigation' not in p.view and 'Upgrade your account' not in p.view
    for i in range(2):
        found=[c for c in p.passages if f'Distinct testimony {i}' in c.text]
        assert found and all(f'Cleric{i}' in c.context and f'12/{i+1}/01' in c.context for c in found)
        assert all(f'Distinct testimony {1-i}' not in c.text for c in found)
    assert 'Real quote 1' in p.view and 'Attachment 1' in p.view


def test_ezboard_guests_damaged_headers_and_nested_data_keep_all_message_content():
    p=plan(ezboard(guest=True,missing_date=True,nested=True),'pub1.ezboard.com')
    assert 'ezboard_posts' in p.exclusions
    for i in range(2):
        found=[c for c in p.passages if f'Distinct testimony {i}' in c.text]
        assert found and all(f'Cleric{i}' in c.context for c in found)
    assert p.view.count('Genuine nested data.')==2
    assert 'Real quote 0' in p.view and 'Attachment 0' in p.view


def test_nonforum_two_column_data_on_ezboard_is_not_mistaken_for_posts():
    raw='<title>Guild guide</title><p>Independent introduction.</p><table><tr><th>Author</th><th>Spells</th></tr><tr><td>Cleric</td><td>Heal 400</td></tr></table>'
    p=plan(raw,'pub1.ezboard.com')
    assert 'ezboard_posts' not in p.exclusions and 'Independent introduction.' in p.view and 'Heal 400' in p.view


def test_ezboard_unknown_broken_row_uses_full_fallback_instead_of_dropping_content():
    raw=ezboard().replace('<td class=m><p>Distinct testimony 1.', '<td>Unexplained cell</td><td class=m><p>Distinct testimony 1.')
    p=plan(raw,'pub1.ezboard.com')
    assert 'Distinct testimony 0.' in p.view and 'Distinct testimony 1.' in p.view and 'Unexplained cell' in p.view
    assert 'ezboard_posts' not in p.exclusions


def test_ezboard_deleted_post_font_heading_is_excluded_but_a_post_quoting_it_is_kept():
    message='ERROR The post you selected no longer exists. It may have been recently deleted.'
    raw='<html><title>www.ezboard.com</title><body><font><b>ERROR</b></font><p>The post you selected no longer exists. It may have been recently deleted.</p><p>Powered by ezboard</p></body></html>'
    p=plan(raw,'pub35.ezboard.com','fclerics.showMessage?topicID=1')
    assert not p.passages and 'archived_access_or_error_page' in p.exclusions
    q=plan(ezboard().replace('Distinct testimony 0.',message),'pub35.ezboard.com','fclerics.showMessage?topicID=1')
    assert message in q.view


def broken_script(quote='"',provider='https://ads.example'):
    return '<script><!--\nfunction makeAd(){document.write('+quote+'<script src='+provider+'/creative.js>'+quote+');document.write('+quote+'</SCRIPT>'+quote+');document.write('+quote+'<iframe src='+provider+'/banner></iframe>'+quote+');}\n//--></script>'


@pytest.mark.parametrize('quote',['"',"'",'`'])
@pytest.mark.parametrize('host',['guild.example','forum.example'])
def test_broken_script_strings_cannot_leak_ad_code_into_any_sites_projection(quote,host):
    raw='<h1>Guild history</h1><p>Opening firsthand testimony.</p>'+broken_script(quote)+'<p>Complete closing testimony.</p>'
    p=plan(raw,host)
    assert 'repaired_script_boundaries' in p.exclusions
    assert 'Opening firsthand testimony.' in p.view and 'Complete closing testimony.' in p.view
    assert 'document.write' not in p.view and 'creative.js' not in p.view and 'banner' not in p.view


def test_script_repair_preserves_code_examples_quotes_regexes_and_multiple_real_articles():
    script=broken_script().replace('function makeAd(){', 'function makeAd(){var punctuation = /[\'\"]/g; var ratio=4 / 2; /* \' comment */')
    literal=html.escape(broken_script())
    raw=script+'<article>First complete article.</article><pre><code>'+literal+'</code></pre><blockquote>'+literal+'</blockquote>'+script+'<p>Final testimony.</p>'
    fixed,n=repair_script_markup(raw)
    assert n==2 and literal in fixed and 'First complete article.' in fixed and 'Final testimony.' in fixed
    p=plan(raw)
    assert 'First complete article.' in p.view and 'Final testimony.' in p.view
    assert any(c.kind=='code' and 'document.write' in c.text for c in p.passages)
    assert any(c.kind=='quote' and 'document.write' in c.text for c in p.passages)


@pytest.mark.parametrize('script',[
 '<script>var text="unterminated;</script><p>Whole real article.</p>',
 '<script>document.write("</script>");<article>Whole real article.</article><script>ready()</script>',
 '<script>document.write("</script>");'+' '*65536+'</script><p>Whole real article.</p>',
 '<script type="text/plain">Unrecognized <p>Internal template.</p></script><p>Whole real article.</p>',
],ids=['unterminated','real_article','bounded_scan','non_javascript_template'])
def test_uncertain_script_boundaries_never_consume_archival_content(script):
    fixed,n=repair_script_markup(script)
    assert n==0 and fixed==script and 'Whole real article.' in fixed
    if len(script)<1024:assert 'Whole real article.' in plan(script).view


def test_script_repair_applies_inside_explicit_content_markers():
    raw='<title>Guild</title><!-- beginContent -->'+broken_script()+'<p>Actual guild evidence.</p><!-- endContent -->'
    p=plan(raw)
    assert 'Actual guild evidence.' in p.view and 'document.write' not in p.view


def test_allakhazam_image_only_body_keeps_its_authored_subject_and_original_reader_link():
    from indexer.html_extraction import website_markdown
    raw=comment('<img src="raid.png">');reader=website_markdown(raw)
    p=passages.prepare(reader,'websites/eqdb.allakhazam.com/20000101000000/item.html',html_source=raw,mime_type='text/html')
    assert 'raid.png' in reader and 'Question about the raid' in p.view
    assert 'empty_allakhazam_comment_body' not in p.exclusions and '900 posts' not in p.view
    passages.verify(reader,p)


def test_allakhazam_anonymous_author_does_not_inherit_profile_ratings():
    raw=comment('A real report mentioning Score: 400.').replace('<a href="poster.html?user=1">Healer</a>900 posts Score: Good','<span>Anonymous</span><br><span>Score</span>: Default [2.00]')
    p=plan(raw,'everquest.allakhazam.com')
    body=next(c for c in p.passages if 'A real report' in c.text)
    assert 'Post by Anonymous' in body.context and 'Default' not in body.context
    assert 'Score: 400' in body.text


@pytest.mark.parametrize('date',['10/17/01 2:08:08 am','damaged'])
def test_ezboard_threaded_view_binds_one_body_without_assigning_reply_directory_to_its_author(date):
    raw=f'''<title>Threaded discussion</title><p>Board navigation</p><table>
    <tr><td>Subject:</td><td colspan=20><span class=title>Original subject</span></td></tr>
    <tr><td>Posted By:</td><td colspan=20><span class=title><a href="uspeaker.showPublicProfile">Speaker</a></span>Member</td></tr>
    <tr><td>Posted At:</td><td colspan=20>{date}</td></tr><tr><td colspan=21></td></tr>
    <tr><td class=m colspan=21>Complete firsthand testimony.<blockquote>Real quotation.</blockquote><table><tr><th>Mana</th><th>Level</th></tr><tr><td>400</td><td>60</td></tr></table></td></tr></table>
    <h3>Replies</h3><table><tr><td>Directory title only</td><td>Other author</td></tr></table>'''
    p=plan(raw,'pub1.ezboard.com')
    assert 'Complete firsthand testimony' in p.view and 'Real quotation' in p.view and '400' in p.view
    if date=='damaged':
        assert 'Directory title only' in p.view and 'ezboard_posts' not in p.exclusions
    else:
        assert 'ezboard_posts' in p.exclusions and 'Directory title only' not in p.view
        assert all('Speaker' in c.context for c in p.passages if 'firsthand testimony' in c.text)
