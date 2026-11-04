"""Structural reproductions of failures observed in live archive indexing.

Bodies and names are synthetic; full capture evidence stays outside the repo.
"""
import pytest
from indexer import passages

RID='websites/independent.example/20000101000000/thread.html'
SOURCE='Complete archival reader, including controls and signatures.'


def plan(html,rid=RID):
    result=passages.prepare(SOURCE,rid,html_source=html,mime_type='text/html')
    passages.verify(SOURCE,result)
    return result


def phpbb(count=2):
    posts=''
    for i in range(count):
        posts+=f'''<tr><td><span class="name"><b>Cleric{i}</b></span>
        <span class="postdetails">Joined: 1999 Posts: 800 Location: Qeynos</span></td>
        <td><span class="postdetails">Posted: Mon Jan {i+1}, 2000 2:30 pm Post subject: Epic help</span>
        <span class="postbody">Unique healing testimony {i}.<br>_________________<br>Repeated guild signature</span></td></tr>'''
    return '<title>Cleric forum :: Epic help</title><p>FAQ Search Memberlist Login</p><table class="forumline">'+posts+'</table><p>Powered by phpBB</p>'


def test_phpbb_posts_are_independent_attributed_passages_without_profile_or_signature_chrome():
    p=plan(phpbb())
    assert len(p.passages)==2
    for i,c in enumerate(p.passages):
        assert c.kind=='post'
        assert f'Cleric{i}' in c.request and f'Jan {i+1}, 2000' in c.request
        assert f'Unique healing testimony {i}' in c.text
        assert f'Unique healing testimony {1-i}' not in c.text
    assert all(x not in p.view for x in ['Posts: 800','Repeated guild signature','Memberlist','Powered by'])


def test_phpbb_long_post_keeps_its_author_and_real_table_and_does_not_absorb_next_author():
    raw=phpbb().replace('Unique healing testimony 0.', '<table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Complete Heal</td><td>400</td></tr></table>'+('Important resurrection evidence. '*500))
    p=plan(raw)
    assert len(p.passages)>3
    assert 'Complete Heal' in p.view and '400' in p.view
    for c in p.passages:
        if 'Important resurrection' in c.text:assert 'Cleric0' in c.request
        assert not ('Unique healing testimony 1' in c.text and 'Important resurrection' in c.text)


def test_unrecognized_postbody_class_retains_unique_surrounding_content():
    p=plan('<title>Guild traditions</title><aside>Essential guild application instructions.</aside><div class="postbody">The raid begins at sunset.</div>')
    assert 'Essential guild application instructions' in p.view
    assert 'raid begins at sunset' in p.view


def test_embedded_forum_widget_cannot_replace_an_independent_article():
    p=plan('<article><h1>Corpse recovery guide</h1><p>Independent article instructions must remain.</p></article>'+phpbb())
    assert 'Independent article instructions must remain' in p.view
    assert 'Unique healing testimony 0' in p.view and 'Unique healing testimony 1' in p.view


def test_wwwthreads_subject_author_date_and_body_survive_without_sponsors():
    raw='<title>Forum</title><p>Visit our sponsor</p>'
    for i in range(2):
        raw+=f'''<table><tr><td class="darktable" rowspan="2"><a name="Post{i}"></a>
        <a href="showprofile.pl?User=Ranger{i}">Ranger{i}</a><span class="small">Member<br>05/11/01 06:26 AM</span></td>
        <td class="subjecttable"><b>Healing discussion {i}</b></td></tr>
        <tr><td class="lighttable"><p class="post">Unique firsthand observation {i}.</p></td></tr></table>'''
    raw+='<p>Forums powered by WWWThreads</p>'
    p=plan(raw)
    assert len(p.passages)==2
    assert 'sponsor' not in p.view and 'WWWThreads' not in p.view
    for i,c in enumerate(p.passages):
        assert c.kind=='post' and f'Ranger{i}' in c.request
        assert '05/11/01 06:26 AM' in c.request and f'Healing discussion {i}' in c.request
        assert f'Unique firsthand observation {i}' in c.text


def test_snitz_message_span_preserves_quotes_and_attribution_not_profile_controls():
    raw='<title>Guild Forum - Bard advice</title><table>'
    for i in range(2):
        raw+=f'''<tr><td><a href="pop_profile.asp?id={i}">Bard{i}</a><small>9000 Posts</small></td>
        <td><table><tr><td><font>Posted - 11/18/2003 : 06:05:53</font><a href="post.asp">Reply with Quote</a></td></tr>
        <tr><td><span id="msg" class="spnMessageText">Evidence from Bard{i}.<blockquote id="quote">Quoted song effect.</blockquote>My own reply {i}.</span></td></tr></table></td></tr>'''
    raw+='</table><p>Snitz Forums</p>'
    p=plan(raw)
    assert '9000 Posts' not in p.view and 'Reply with Quote' not in p.view
    assert 'Quoted song effect.' in p.view and 'My own reply 1.' in p.view
    assert all('Bard0' not in c.request for c in p.passages if 'My own reply 1' in c.text)
    assert all('11/18/2003' in c.request for c in p.passages)


def test_unclosed_wwwthreads_paragraph_cannot_reassign_the_next_message_to_the_first_author():
    def post(i):
        return f'<table><tr><td rowspan="2"><a name="Post{i}"></a><a href="showprofile.pl?User=Healer{i}">Healer{i}</a>01/02/2000 3:45 PM</td><td class="subjecttable"><b>Message {i}</b></td></tr><tr><td><p class="post">Unique testimony by Healer{i}.'
    p=plan('<title>Old forum</title>'+post(0)+post(1)+'</p></td></tr></table></p></td></tr></table>')
    assert len(p.passages)==2
    for i,c in enumerate(p.passages):
        assert f'Healer{i}' in c.request
        assert f'Healer{1-i}' not in c.text


def test_unregistered_wwwthreads_author_without_profile_link_is_not_discarded():
    raw='''<title>Guest reply</title><table><tr><td rowspan="2"><a name="Post3"></a>Guest Healer<span class="small">(Unregistered)<br>01/02/2000 3:45 PM</span></td><td class="subjecttable"><b>Reply</b></td></tr><tr><td><p class="post">Important guest testimony.</p></td></tr></table>'''
    p=plan(raw)
    assert len(p.passages)==1
    assert 'Important guest testimony.' in p.view and 'Guest Healer' in p.passages[0].context
    assert 'Unregistered' not in p.passages[0].context


def test_partially_recognized_phpbb_thread_retains_unmatched_message_body():
    p=plan(phpbb(1).replace('</table>','<tr><td><span class="postbody">Additional evidence from a damaged post header.</span></td></tr></table>'))
    assert 'Unique healing testimony 0' in p.view
    assert 'Additional evidence from a damaged post header' in p.view


def test_post_author_survives_authored_subheadings_and_quotes_do_not_label_the_own_reply():
    raw=phpbb(1).replace('Unique healing testimony 0.','<h3>My raid notes</h3>'+('Healing advice. '*300)+'<table><tr><td><b>Another healer wrote:</b></td></tr><tr><td class="quote">Quoted first-hand testimony.</td></tr></table><p>My own reply follows.</p>')
    p=plan(raw)
    assert all('Post by Cleric0' in c.context for c in p.passages)
    quote=[c for c in p.passages if c.kind=='quote']
    assert quote and 'Another healer wrote:' in ''.join(c.text for c in quote)
    reply=[c for c in p.passages if 'My own reply follows' in c.text]
    assert reply and all('Quoted correspondence' not in c.context for c in reply)


def test_punctuation_only_quotation_keeps_a_separate_attribution_boundary():
    p=plan(phpbb(1).replace('Unique healing testimony 0.','<p>My reply.</p><blockquote>???</blockquote><p>Another reply.</p>'))
    quote=[c for c in p.passages if c.kind=='quote']
    assert len(quote)==1 and '???' in quote[0].text
    assert 'My reply.' not in quote[0].text and 'Another reply.' not in quote[0].text


def test_ezboard_promotion_icon_does_not_hide_author_and_footer_is_not_a_reply():
    raw='''<title>Cleric discussion</title><table><tr><td>Author</td><td>Comment</td></tr>
    <tr><td><a href="promotion"><img src="badge.gif"></a><a href="board.showUserPublicProfile?gid=healer">Healer</a><br>Posts: 50 (1/2/2000 3:45 pm)</td>
    <td>Useful cleric testimony.</td></tr></table><p>Powered by ezboard. Copyright. Topic Commands</p>'''
    p=plan(raw,'websites/pub3.ezboard.com/20000101000000/fclerics.showMessage?topicID=3')
    assert len(p.passages)==1
    assert 'Healer' in p.passages[0].request and '1/2/2000' in p.passages[0].request
    assert 'Topic Commands' not in p.view


@pytest.mark.parametrize('raw,rid',[
    ('<title>System Message: Error</title><h1>Error</h1><p>The post you selected no longer exists.</p>','fboard.showMessage'),
    ('<title>Untitled</title><p>phpBB : <b>Critical Error</b></p><p>Could not connect to the database</p>','viewtopic.php'),
    ('<title>Forum</title><table class="forumline"><tr><th>Information</th></tr><tr><td>The topic or post you requested does not exist</td></tr></table><p>Powered by phpBB</p>','viewtopic.php'),
    ('<title>Private forums</title><h2>vBulletin Message</h2><p>You are not logged in or you do not have permission to access this page.</p><form><input type="password"></form>','showthread.php'),
    ('<title>Untitled</title><h1>Site Temporarily Disabled</h1><p>This site has been disabled. If you are the site owner please call support.</p>','index.html'),
    ('<title>Make a New Post - Forum</title><form action="newpost.pl"><input name="Subject"><textarea name="Body"></textarea></form><p>Fill out the form below to post a message.</p>','newpost.pl'),
    ('<title>New user</title><form action="newuser.pl"><input name="Username"><input type="password"></form><p>Enter the following information to register a Username.</p>','newuser.pl'),
])
def test_recognized_error_and_posting_pages_retain_reader_hash_without_semantic_passages(raw,rid):
    p=plan(raw,'websites/any-forum.example/20000101000000/'+rid)
    assert p.passages==()
    assert p.source_sha256==passages.digest(SOURCE)
    assert set(p.exclusions)&{'archived_access_or_error_page','forum_utility_page'}


def test_error_text_in_real_article_or_real_post_is_preserved():
    text='The topic or post you requested does not exist'
    for raw in [f'<title>404 Not Found</title><article><h1>Solving errors</h1><p>{text}: here is how to repair it.</p></article>',phpbb(1).replace('Unique healing testimony 0.',text+' is a warning that I saw yesterday.')]:
        p=plan(raw)
        assert text in p.view and p.passages


def test_real_data_headers_repeat_and_adjacent_tables_have_distinct_context():
    first='<table><tr><td></td><td></td><td></td></tr><tr><td>Faction:</td><td>Positive Quests:</td><td>Negative Quests:</td></tr>'+''.join(f'<tr><td>Faction {i}</td><td>{i}</td><td>{i+1}</td></tr>' for i in range(100))+'</table>'
    second='<table><tr><th>Spell</th><th>Mana</th></tr>'+''.join(f'<tr><td>Healing spell {i}</td><td>{i}</td></tr>' for i in range(100))+'</table>'
    p=plan('<title>Data reference</title>'+first+second)
    assert len(p.passages)>4
    for c in p.passages:
        if 'Faction ' in c.text:assert 'Positive Quests' in c.context and 'Negative Quests' in c.context and 'Mana' not in c.context
        if 'Healing spell' in c.text:assert 'Spell' in c.context and 'Mana' in c.context and 'Positive Quests' not in c.context
        if c.kind=='table':assert any(char.isalnum() for char in c.context.split('Table columns:')[-1])


def test_short_equipment_sections_pack_together_without_losing_labels_or_values():
    raw='<title>Outfit</title>'+''.join(f'<h2>{slot}</h2><p>{item}</p>' for slot,item in [('Ear 1','Silver Earring'),('Ear 2','Hammer Earring'),('Neck','Dragon Necklace'),('Finger','Platinum Ring')])
    p=plan(raw)
    assert len(p.passages)==1
    assert all(v in p.view for v in ['Ear 1','Silver Earring','Ear 2','Hammer Earring','Neck','Dragon Necklace','Finger','Platinum Ring'])


def test_numbered_reply_after_wrapped_quote_is_not_attributed_to_quoted_speaker():
    source='# Subject: Support\n\n### From: The author\n\n---\n\nOther player wrote:\n> A quoted complaint about this\ngame\n1. My own reply starts here.\n2. My second point.\nSigned, the author.'
    p=passages.prepare(source,'newsgroups/eq/1.txt')
    passages.verify(source,p)
    quote=[c for c in p.passages if c.kind=='quote']
    assert quote and all('My own reply' not in c.text for c in quote)
    reply=[c for c in p.passages if 'My own reply' in c.text]
    assert reply and all('Quoted correspondence' not in c.context for c in reply)
    assert 'game' in ''.join(c.text for c in quote)


def test_fenced_code_does_not_create_fake_quotes_tables_or_section_headings():
    source='A technical EQ site.\n\n```text\n# literal heading\n> literal prompt\n| literal | pipe |\n_________________\n```\n\nActual article continues.'
    p=passages.prepare(source,RID)
    passages.verify(source,p)
    code=[c for c in p.passages if c.kind=='code']
    assert len(code)==1 and '> literal prompt' in code[0].text
    assert all('Quoted correspondence' not in c.context and 'Table columns:' not in c.context for c in p.passages)
    assert 'Actual article continues' in p.view


def test_small_aggregator_database_menu_is_not_a_standalone_data_chunk():
    raw='<title>Factions</title><table><tr><td>'+''.join(f'<a href="/{i}">{name}</a>' for i,name in enumerate(['By Item','By Zone','By Faction']))+'</td></tr></table><table><tr><th>Faction</th><th>Positive Quests</th></tr><tr><td>Clerics of Dawn</td><td>12</td></tr></table>'
    p=plan(raw,'websites/everquest.allakhazam.com/20000101000000/db/flist.html')
    assert 'By Item' not in p.view and 'Clerics of Dawn' in p.view
    assert len(p.passages)==1


def test_menu_links_sharing_a_table_with_numeric_data_do_not_discard_the_data_grid():
    links=''.join(f'<a href="/{i}">{name}</a>' for i,name in enumerate(['By Item','By Zone','By Faction']))
    raw='<title>Faction reference</title><table><tr><td colspan="3">'+links+'</td></tr><tr><td>Faction:</td><td>Positive Quests:</td><td>Negative Quests:</td></tr><tr><td>Clerics of Dawn</td><td>12</td><td>0</td></tr></table>'
    p=plan(raw,'websites/everquest.allakhazam.com/20000101000000/db/flist.html')
    assert 'Clerics of Dawn' in p.view and '12' in p.view


def test_trailing_rule_cannot_clear_the_data_table_context():
    raw='<title>Spell reference</title><table><tr><th>Spell</th><th>Mana</th></tr>'+''.join(f'<tr><td>Healing spell {i}</td><td>{i}</td></tr>' for i in range(60))+'</table><hr>'
    p=plan(raw)
    assert all('Spell' in c.context and 'Mana' in c.context for c in p.passages if c.kind=='table')


def test_prose_in_a_layout_table_is_not_invented_as_column_names():
    p=plan('<title>December news</title><table><tr><td>Monday December 13, 1999</td><td>'+('Our guild recovered the corpse and learned a new raid strategy. '*100)+'</td></tr></table>')
    assert 'learned a new raid strategy' in p.view
    assert all('Table columns:' not in c.context for c in p.passages)
