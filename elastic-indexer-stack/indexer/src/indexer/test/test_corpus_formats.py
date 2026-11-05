"""Regressions for shared forum/database layouts observed in large captures."""
import pytest

from indexer import passages


def plan(raw, path='thread.html', host='unrelated.example'):
    reader='Complete archival reader, with original controls and all messages.'
    p=passages.prepare(reader,f'websites/{host}/20000101000000/{path}',
                       html_source=raw,mime_type='text/html')
    passages.verify(reader,p)
    return p


def attributed(p, text, author):
    chunks=[c for c in p.passages if text in c.text]
    assert chunks and all(c.kind in ('post','quote','table','code') and author in c.context for c in chunks)


def snitz_review(old=False, damaged=False):
    raw='''<title>Necromancer leveling</title><p>Snitz Forums</p>
    <form><input type=password><textarea name=Message>[quote]Captured draft quotation.[/quote]</textarea></form>
    <p>Format Mode: Smilies Insert Speech Icon</p><table><tr><td colspan=2>T O P I C R E V I E W</td></tr>'''
    for i in range(2):
        body=f'<p>Distinct leveling advice {i}.</p><blockquote>Genuine quote {i}.</blockquote><a href="notes.txt">Notes {i}</a>'
        if not old:body=f'<small>Posted - 01/02/2000 : 03:04:05 PM</small><hr><span class=spnMessageText>{body}</span>'
        raw+=f'<tr><td nowrap valign=top width=100><b>Scholar{i}</b></td><td valign=top>{body}</td></tr>'
        if damaged:raw+=f'<p>Continuation outside the damaged row {i}.</p>'
    return raw+'</table><p>Copyright Snitz Forums</p>'


@pytest.mark.parametrize('old,damaged',[(False,False),(True,False),(True,True)])
def test_snitz_review_keeps_draft_and_every_reviewed_message_with_boundaries(old,damaged):
    p=plan(snitz_review(old,damaged),'post.asp?method=ReplyQuote')
    assert 'Captured draft quotation.' in p.view
    assert 'Format Mode' not in p.view and 'Insert Speech Icon' not in p.view
    for i in range(2):
        attributed(p,f'Distinct leveling advice {i}.',f'Scholar{i}')
        assert f'Genuine quote {i}.' in p.view and f'Notes {i}' in p.view
        if damaged:assert f'Continuation outside the damaged row {i}.' in p.view
    assert not any('Distinct leveling advice 0.' in c.text and 'Distinct leveling advice 1.' in c.text for c in p.passages)


def test_unrecognized_review_never_selects_only_the_populated_quote_field():
    raw=snitz_review().replace('nowrap valign=top width=100','class=unknown').replace('class=spnMessageText','class=unknown')
    p=plan(raw,'post.asp?method=ReplyQuote')
    assert all(s in p.view for s in ('Captured draft quotation.','Distinct leveling advice 0.','Distinct leveling advice 1.'))


@pytest.mark.parametrize('old',[False,True])
def test_damaged_review_heading_cannot_drop_visible_message_rows(old):
    raw=snitz_review(old).replace('T O P I C R E V I E W','Damaged review heading')
    p=plan(raw,'post.asp?method=ReplyQuote')
    assert all(s in p.view for s in ('Captured draft quotation.','Distinct leveling advice 0.','Distinct leveling advice 1.'))


def test_mixed_snitz_templates_retain_messages_with_missing_span_markup():
    raw=snitz_review().replace('T O P I C R E V I E W','Damaged review heading')
    raw=raw.replace('class=spnMessageText','class=unknown',1)
    p=plan(raw,'post.asp?method=ReplyQuote')
    assert all(s in p.view for s in ('Captured draft quotation.','Distinct leveling advice 0.','Distinct leveling advice 1.'))


def test_old_snitz_topic_rows_keep_post_author_and_body_after_the_metadata_separator():
    raw='<title>Forum</title><p>Snitz Forums</p><table>'
    for i in range(2):
        raw+=f'''<tr><td nowrap><a href="pop_profile.asp?id={i}">Scholar{i}</a>900 posts</td>
        <td colspan=2><font>Posted - 01/02/2000 : 03:04:05</font><a href=post.asp>Reply with Quote</a><hr>
        <p>Old topic testimony {i}.</p><table><tr><th>Level</th><th>Zone</th></tr><tr><td>20</td><td>Guk</td></tr></table></td></tr>'''
    p=plan(raw+'</table>','topic.asp?TOPIC_ID=1')
    assert '900 posts' not in p.view and 'Reply with Quote' not in p.view
    for i in range(2):attributed(p,f'Old topic testimony {i}.',f'Scholar{i}')
    assert 'Guk' in p.view and '20' in p.view


def test_guest_snitz_topic_with_a_damaged_timestamp_keeps_its_message():
    raw='''<p>Snitz Forums</p><table><tr><td nowrap><b>Guest Scholar</b></td>
    <td>Posted - [damaged date]<hr><p>Important guest testimony.</p></td></tr></table>'''
    p=plan(raw,'topic.asp?TOPIC_ID=1')
    attributed(p,'Important guest testimony.','Guest Scholar')


def vb2(shared_metadata_table=False):
    raw='<title>Cleric discussion</title><p>Powered by vBulletin 2.3.0</p>'
    metadata_end='</td></tr><tr><td colspan=2>' if shared_metadata_table else '</td></table>'
    body_end='</td></tr></table>' if shared_metadata_table else ''
    for i in range(2):
        raw+=f'''<!-- Postbit Header --><table><tr><td><b>Healing advice</b></td><td><a href="showthread.php?postid={i}#post{i}">Post #{i+1} of 2</a></td></tr></table><!-- Postbit Header -->
        <!-- Postbit User Info --><td><b>Healer{i}</b><table><tr><td>Warning Level: 0; registered 1999; 900 posts</td></tr></table></td><!-- Postbit User Info -->
        <!-- Main Post Body --><table><td><img alt="Old Post"></td><td>Posted 01-02-2000 03:04 PM{metadata_end}
        <p>Distinct healing evidence {i}.</p><blockquote>Actual quoted reply.</blockquote>
        <table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Heal</td><td>400</td></tr></table>
        {body_end}<!-- Main Post Body -->'''
    return raw


@pytest.mark.parametrize('shared_metadata_table',[False,True])
def test_old_vbulletin_source_comment_regions_keep_posts_without_profile_tables(shared_metadata_table):
    p=plan(vb2(shared_metadata_table))
    assert 'Warning Level' not in p.view and '900 posts' not in p.view and 'Old Post' not in p.view
    for i in range(2):attributed(p,f'Distinct healing evidence {i}.',f'Healer{i}')
    assert 'Actual quoted reply.' in p.view and '400' in p.view


def test_literal_signature_fences_cannot_swallow_later_post_attribution():
    raw=snitz_review().replace('Distinct leveling advice 0.','Distinct leveling advice 0.<br>~~~ Signature ~~~')
    p=plan(raw,'post.asp?method=ReplyQuote')
    attributed(p,'Distinct leveling advice 1.','Scholar1')
    assert all('Scholar0' not in c.context for c in p.passages if 'Distinct leveling advice 1.' in c.text)
    assert not any(c.kind=='code' and 'Distinct leveling advice 1.' in c.text for c in p.passages)


def test_phpbb_signature_separator_is_not_a_body_truncation_rule_for_other_engines():
    raw=snitz_review().replace('Distinct leveling advice 0.','Distinct leveling advice 0.<br>_________________<br>Petition character and server testimony.')
    p=plan(raw,'post.asp?method=ReplyQuote')
    assert 'Petition character and server testimony.' in p.view
    attributed(p,'Petition character and server testimony.','Scholar0')


def test_forum_template_cells_are_not_fake_tables_but_real_authored_tables_remain():
    from indexer.passage_forums import section
    raw=section('Scholar','01/02/2000','<td>Real message.<table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Heal</td><td>400</td></tr></table></td>')
    p=plan(raw)
    assert any(c.kind=='post' and 'Real message.' in c.text for c in p.passages)
    assert any(c.kind=='table' and '400' in c.text for c in p.passages)


def test_damaged_vbulletin_region_does_not_drop_a_message_or_inherit_the_previous_author():
    p=plan(vb2().replace('<!-- Main Post Body -->','',1))
    assert 'Distinct healing evidence 0.' in p.view and 'Distinct healing evidence 1.' in p.view


def test_vbulletin_archive_pairs_do_not_require_numbered_user_and_body_ids():
    raw='''<title>Forum archive</title><div class=largefont>View Full Version: <a href="showthread.php?t=1">Epic help</a></div><div class=pagebody>'''
    for i in range(2):
        raw+=f'''<div class=post><div class=posttop><div class=username>Healer{i}</div><div class=date>01-02-2000, 03:04 PM</div></div>
        <div class=posttext>Archive evidence {i}.<a href=raid.txt>Raid attachment</a></div></div>'''
    p=plan(raw+'</div><p>vBulletin v3.0.5 Copyright</p>')
    for i in range(2):attributed(p,f'Archive evidence {i}.',f'Healer{i}')
    assert 'Raid attachment' in p.view and 'Copyright' not in p.view


@pytest.mark.parametrize('damaged_header',[False,True])
def test_vbhome_archive_rows_keep_all_bodies_without_inventing_dates(damaged_header):
    raw='<table class=tbarchive><tr><td>Epic help - Click <a href="showthread.php?threadid=1">HERE</a> for Original Thread</td></tr><tr><td>'
    for i in range(2):
        role='unknown' if damaged_header and i==1 else 'archiveheader'
        raw+=f'''<table class=tbborder><tr><td class={role}>Scholar{i}</td></tr><tr class=archivebody><td>Archived evidence {i}.<a href=raid.txt>Raid notes</a></td></tr></table>'''
    p=plan(raw+'</td></tr></table>')
    assert p.title=='Epic help' and 'Archived evidence 0.' in p.view and 'Archived evidence 1.' in p.view
    attributed(p,'Archived evidence 0.','Scholar0')
    if not damaged_header:attributed(p,'Archived evidence 1.','Scholar1')
    else:assert all('Scholar0' not in c.context for c in p.passages if 'Archived evidence 1.' in c.text)
    assert all('2000' not in c.context for c in p.passages)


def test_phpbb_printer_template_keeps_individual_authors_and_quotes():
    raw='<title>Printed thread</title><span class=maintitle>Epic help</span><p>Powered by phpBB</p><table>'
    for i in range(2):
        raw+=f'''<tr><td class=printline><span class=postdetails>#{i+1}: <b>Epic help</b> Author: <b>Healer{i}</b>, Posted: Mon Jan 2, 2000 3:04 pm</span></td></tr>
        <tr><td class=printline><span class=postbody>Print evidence {i}.<blockquote>Quoted evidence.</blockquote></span></td></tr>'''
    p=plan(raw+'</table>','viewtopic.php?printertopic=1')
    for i in range(2):attributed(p,f'Print evidence {i}.',f'Healer{i}')
    assert 'Quoted evidence.' in p.view and 'Powered by' not in p.view


def test_lithium_printer_template_accepts_styled_username_instead_of_a_profile_link():
    raw='<title>Epic help</title>'
    for i in range(2):
        raw+=f'''<table><tr><td></td><td>Epic help</td></tr><tr><td class=msg_user_cell><span style="font-weight:bold">Healer{i}</span><br>Member</td>
        <td class=msg_text_cell rowspan=2><p>Printed healing evidence {i}.</p></td></tr><tr><td class=msg_date_cell><span class=date_text>01-02-2000</span> <span class=time_text>03:04 PM</span></td></tr></table>'''
    p=plan(raw,'eq/board/print?format=all')
    for i in range(2):attributed(p,f'Printed healing evidence {i}.',f'Healer{i}')


def aspnet():
    raw='<title>Forum</title><p>Site menu</p><table class=PanelBorder id=Forum_PostList><tr><th class=PanelHeader>Author</th><th class=PanelHeader>Thread: Epic help</th></tr>'
    for i in range(2):
        raw+=f'''<tr><td class=PanelBody><a href="/User/UserProfile.aspx?UserName=Healer{i}">Healer{i}</a>Joined: 1999 Total Posts: 900</td>
        <td class=PanelBody><table><tr><td><span class=SmallBoldText>Epic help<a name="{100+i}"></a></span><span class=SmallText>Posted: Jun 6 03 3:57 PM</span></td></tr>
        <tr><td colspan=2><span>ASP forum evidence {i}.<blockquote>Genuine quotation.</blockquote><a href=raid.txt>Raid notes</a></span></td></tr>
        <tr><td colspan=2><span class=SmallText><hr>Repeated guild signature</span></td></tr></table></td></tr>'''
    return raw+'</table>'


def test_aspnet_forum_roles_preserve_messages_dates_and_attachments_without_profiles():
    p=plan(aspnet())
    for i in range(2):
        attributed(p,f'ASP forum evidence {i}.',f'Healer{i}')
        assert any('Jun 6 03 3:57 PM' in c.context for c in p.passages if f'ASP forum evidence {i}.' in c.text)
    assert 'Genuine quotation.' in p.view and 'Raid notes' in p.view
    assert all(t not in p.view for t in ('Total Posts','Site menu','Repeated guild signature'))


def test_damaged_aspnet_message_keeps_both_bodies_and_unknown_surrounding_articles():
    raw=aspnet().replace('name="101"','name="damaged"')
    p=plan('<article>Independent raid application instructions.</article>'+raw)
    assert all(t in p.view for t in ('ASP forum evidence 0.','ASP forum evidence 1.','Independent raid application'))


def ultra():
    raw='<title>Maximum forum</title>'
    for i in range(2):
        raw+=f'''<table><tr><td><table><tr><td><img alt="New Message"></td><td><b>Epic help (modified 0 times)</b></td><td><b>Healer{i}</b></td></tr></table></td></tr>
        <tr><td><a href="UltraBoard.cgi?Action=ShowProfile&ID={i}">Profile</a></td></tr>
        <tr><td><table><tr><td>UltraBoard evidence {i}.<blockquote>Real quote.</blockquote><a href=raid.txt>Raid notes</a></td></tr></table></td></tr>
        <tr><td>Aug 06, 2000 12:23:16 P.M.</td></tr></table>'''
    return raw


def test_ultraboard_message_blocks_are_attributed_without_control_tables():
    p=plan(ultra())
    for i in range(2):attributed(p,f'UltraBoard evidence {i}.',f'Healer{i}')
    assert all(t not in p.view for t in ('modified 0 times','New Message','Profile'))
    assert 'Raid notes' in p.view and 'Real quote.' in p.view


def test_ambiguous_ultraboard_layout_keeps_all_source_messages():
    p=plan(ultra().replace('Aug 06, 2000 12:23:16 P.M.','Damaged date',1))
    assert 'UltraBoard evidence 0.' in p.view and 'UltraBoard evidence 1.' in p.view


def everlore():
    raw='<title>Database</title><h1>Lantern of Dawn</h1><table><tr><th>Item</th><th>Wisdom</th></tr><tr><td>Lantern</td><td>9</td></tr></table><table>'
    for i in range(2):
        raw+=f'''<tr><td nowrap><b>Submited by: <a href="mailto:healer@example">Healer{i}</a></b><font>On: 1/2/2000 3:04:05 PM</font>
        <td align=right><b>Rank:</b> worst <a href="javascript:openWindow(1,{i})">1</a> best</td></tr>
        <tr><td colspan=2><p>Distinct item evidence {i}.</p><blockquote>Actual item quote.</blockquote><a href=item.txt>Item attachment</a></td></tr>'''
    return raw+'</table>'


def test_everlore_comments_keep_entity_data_and_independent_author_date_context():
    p=plan(everlore(),host='www.everlore.com')
    assert p.title=='Lantern of Dawn' and 'Wisdom' in p.view and '9' in p.view
    assert 'Rank:' not in p.view and 'worst' not in p.view
    for i in range(2):attributed(p,f'Distinct item evidence {i}.',f'Healer{i}')
    assert 'Actual item quote.' in p.view and 'Item attachment' in p.view


def test_everlore_damaged_comment_row_keeps_all_body_text():
    p=plan(everlore().replace('colspan=2','colspan=unknown',1),host='www.everlore.com')
    assert 'Distinct item evidence 0.' in p.view and 'Distinct item evidence 1.' in p.view


@pytest.mark.parametrize('path', ['fhealers.showAddReplyScreenFromWeb?topicID=1','fhealers.showMessageRange?topicID=1'])
def test_deleted_ezboard_error_is_excluded_independently_of_the_route(path):
    p=plan('<title>www.ezboard.com</title><body><b>ERROR</b><p>The post you selected no longer exists. It may have been recently deleted.</p><p>Copyright ezboard</p></body>',path,host='pub10.ezboard.com')
    assert not p.passages and 'archived_access_or_error_page' in p.exclusions


@pytest.mark.parametrize('raw',[
    '<title>Forum</title><h1>vBulletin Message</h1><blockquote>No Thread specified. If you followed a valid link, please notify the administrator.</blockquote><p>Powered by vBulletin</p>',
    '<title>Forum</title><h2>System Message</h2><h1>Unregistered Users Restricted from Viewing</h1><p>Please log in to your ezboard account to view this community.</p>',
    '<b>Warning</b>: main(): failed to open stream: No such file or directory in <b>/srv/forum/archive.php</b> on line 27<br><b>Fatal error</b>: main(): Failed opening required functions.php in /srv/forum/archive.php on line 27',
])
def test_generated_forum_and_php_error_layouts_do_not_create_search_passages(raw):
    p=plan(raw)
    assert not p.passages and 'archived_access_or_error_page' in p.exclusions


@pytest.mark.parametrize('error',['No Thread specified.','Unregistered Users Restricted from Viewing','Fatal error: Failed opening required functions.php'])
def test_genuine_articles_and_quoted_troubleshooting_are_not_treated_as_server_errors(error):
    p=plan('<title>Troubleshooting</title><article><h1>How to recover a forum</h1><blockquote>'+error+'</blockquote><p>These are my recovery instructions.</p></article>')
    assert error in p.view and 'recovery instructions' in p.view and p.passages


def test_php_warning_prefix_and_fatal_footer_do_not_erase_an_intervening_article():
    raw='''<b>Warning</b>: main(): failed to open stream: No such file or directory in /srv/header.php on line 2
    <h1>Guild resurrection guide</h1><p>Distinct advice on recovering the raid.</p>
    <b>Fatal error</b>: main(): Failed opening required footer.php in /srv/footer.php on line 27'''
    p=plan(raw)
    assert 'Distinct advice on recovering the raid.' in p.view and p.passages


def test_error_heading_cannot_erase_a_populated_reply_quote():
    p=plan('<title>System Message: Error</title><h1>Error</h1><textarea>Only surviving healing testimony.</textarea>','post.asp')
    assert p.passages and 'Only surviving healing testimony.' in p.view
