"""Synthetic reproductions of shared templates observed across archive hosts."""
import pytest
from indexer import passages
from indexer.html_extraction import website_markdown

RID='websites/unrelated.example/20000101000000/thread.html'

def plan(raw,rid=RID):
    result=passages.prepare('Complete archival reader',rid,html_source=raw,mime_type='text/html')
    passages.verify('Complete archival reader',result)
    return result


@pytest.mark.parametrize('inner',['','Reply'])
def test_nested_control_links_keep_surrounding_content_after_parent_removal(inner):
    raw=('<title>Guild history</title><p>Opening guild raid testimony.</p>'
         '<a href="reply"><a href="nested">'+inner+'</a>'+('Reply' if not inner else '')+'</a>'
         '<p>Complete closing testimony with <a href="guide">a useful guide</a>.</p>')
    result=plan(raw)
    assert 'Opening guild raid testimony.' in result.view
    assert 'Complete closing testimony' in result.view and 'a useful guide' in result.view
    assert 'forum_or_account_control' in result.exclusions


def test_skinned_vbulletin_word_dates_and_guest_menus_have_independent_posts():
    raw='<title>Epic discussion</title><p>Search Home FAQ</p>'
    for i,name in enumerate(('Guest Healer','Guild Ranger')):
        raw+=f'''<table class=tborder><tr><td class=thead><a name=post{100+i}></a>November {i+1}th, 2004, 11:33 PM</td></tr>
        <tr><td><div id=postmenu_{100+i}>{name}</div><small>Posts: 10000</small></td>
        <td><!-- message --><p>Distinct testimony {i}.</p><blockquote>Preserved quotation {i}.</blockquote>
        <table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Heal</td><td>400</td></tr></table><!-- / message --></td></tr></table>'''
    p=plan(raw)
    assert 'vbulletin_posts' in p.exclusions and 'Posts: 10000' not in p.view
    for i,name in enumerate(('Guest Healer','Guild Ranger')):
        body=[c for c in p.passages if f'Distinct testimony {i}' in c.text]
        assert body and all(name in c.context and 'November' in c.context for c in body)
    assert 'Preserved quotation 1' in p.view and '400' in p.view


def vb2(broken=False):
    raw='<title>Warrior mitigation</title>'
    for i in range(2):
        raw+=f'''<div id=p{10+i}h><b>Fighter{i}</b>11-03-2003 08:22 PM</div>
        <div id=p{10+i}e><table><tr><td><a name=post{10+i}></a><b>Fighter{i}</b>Registered: 1999 Posts: 900</td>
        <td><b>Warrior mitigation</b><p>Complete testimony {i}.<a href=attachment.php?id={i}>raid-note-{i}.txt</a></p></td></tr></table></div>'''
    return raw.replace('id=p11h','id=unknown') if broken else raw


def test_vbulletin_two_expanded_bodies_do_not_repeat_collapsed_profile_metadata():
    p=plan(vb2())
    assert len(p.passages)==2 and all(c.kind=='post' for c in p.passages)
    assert 'Registered:' not in p.view and 'Posts: 900' not in p.view
    assert all(f'Fighter{i}' in c.context and f'Complete testimony {i}' in c.text for i,c in enumerate(p.passages))
    assert 'raid-note-1.txt' in p.view


def test_damaged_vbulletin_pair_keeps_both_messages_in_generic_fallback():
    p=plan(vb2(True))
    assert 'Complete testimony 0' in p.view and 'Complete testimony 1' in p.view


def ubb():
    raw='<title>Forum</title><p>Powered by Ultimate Bulletin Board</p><table><tr><th>Author</th><th>Topic: Bard advice</th></tr>'
    for i in range(2):
        raw+=f'''<tr><td><b>Bard{i}</b>Member # 55</td><td><font>posted August 01, 2001 02:07 AM <a href=profile>Profile</a></font><hr>
        <font>Firsthand observation {i}.<blockquote>Quote {i}.</blockquote><table><tr><th>Song</th><th>Mana</th></tr><tr><td>Hymn</td><td>0</td></tr></table></font>
        <hr><font>Posts: 800 | IP: Logged</font></td></tr>'''
    return raw+'</table><p>Contact Search FAQ</p>'


def test_ubb_classic_uses_author_topic_structure_across_hosts_not_domain_rules():
    p=plan(ubb())
    assert 'ubbclassic_posts' in p.exclusions and p.title=='Bard advice'
    assert all(s not in p.view for s in ('Member #','IP: Logged','Posts: 800','Contact Search FAQ'))
    assert 'Hymn' in p.view and 'Quote 1' in p.view
    for i in range(2):
        body=[c for c in p.passages if f'Firsthand observation {i}' in c.text]
        assert body and all(f'Bard{i}' in c.context and 'August 01, 2001' in c.context for c in body)


def test_damaged_ubb_body_falls_back_without_discarding_other_posts():
    p=plan(ubb().replace('posted August 01, 2001','damaged date',1))
    assert 'Firsthand observation 0' in p.view and 'Firsthand observation 1' in p.view


def test_lithium_messages_keep_authors_dates_quoted_tables_and_guest_text():
    raw='<title>Beastlord class issues</title><p>Station global menu</p>'
    for i in range(2):
        raw+=f'''<table><tr><td class=subjectbar width="100%">Class issue {i}</td></tr>
        <tr><td class=msg_user_cell><a class=auth_text href="/eq/view_profile?user.id={i}">Player{i}</a>Posts: 800</td>
        <td class=msg_text_cell><p>Unique balance evidence {i}.</p><blockquote>Quote {i}.</blockquote><table><tr><th>Level</th><th>Damage</th></tr><tr><td>60</td><td>400</td></tr></table></td></tr>
        <tr><td class=msg_date_cell><span class=date_text>07-09-2004</span> 05:45 PM</td></tr></table>'''
    p=plan(raw)
    assert 'lithium_posts' in p.exclusions
    assert 'Station global menu' not in p.view and 'Posts: 800' not in p.view
    for i in range(2):
        chunks=[c for c in p.passages if f'Unique balance evidence {i}' in c.text]
        assert chunks and all(f'Player{i}' in c.context and '07-09-2004' in c.context and f'Class issue {i}' in c.context for c in chunks)
    assert '400' in p.view and 'Quote 1' in p.view


def test_forum_widget_never_replaces_an_independent_article():
    p=plan('<article><h1>Independent guild rules</h1><p>Unique application requirements.</p></article>'+ubb())
    assert 'Unique application requirements' in p.view and 'Firsthand observation 1' in p.view


def test_bestiary_entity_heading_wins_over_sidebar_and_new_comment_layout_is_attributed():
    raw='''<title>Illia Bestiary</title><table><tr><td id=headerrow>Bestiary:</td></tr></table>
    <h2>Ancient Dragon</h2><p>Minimum Level: 60 Maximum Level: 65</p>
    <table class=mform><form><tr><td id=headerrow colspan=2>Dragon tactics<small>Posted @ Jan 1 2000</small></td></tr>
    <tr><td><span>By:</span><a href=poster.html?user=1>Healer</a>900 posts Score: Good</td>
    <td>Complete dragon strategy.<table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Heal</td><td>400</td></tr></table></td></tr><tr><td>0 Replies</td></tr></form></table>'''
    p=plan(raw,'websites/eqbeastiary.allakhazam.com/20000101000000/search.shtml?id=1')
    assert p.title=='Ancient Dragon' and 'Minimum Level: 60' in p.view
    chunks=[c for c in p.passages if 'Complete dragon strategy' in c.text]
    assert chunks and all('Healer' in c.context and 'Jan 1 2000' in c.context for c in chunks)
    assert '900 posts' not in p.view and '0 Replies' not in p.view and '400' in p.view


def test_allakhazam_sidebar_removed_without_losing_catalog_or_numeric_data():
    raw='<title>Bestiary</title><table><tr><td>'
    raw+='<table>'+''.join('<tr><td id=headerrow>'+name+':</td></tr><tr><td><form><select><option>Pick</option></select></form></td></tr>' for name in ('Bestiary','Jump','Statistics'))+'</table>'
    raw+='</td><td><h2>Ancient Dragon</h2><table><tr><th>Faction</th><th>Value</th></tr><tr><td>Dragons</td><td>400</td></tr></table></td></tr></table>'
    p=plan(raw,'websites/eqbeastiary.allakhazam.com/20000101000000/search.shtml?id=1')
    assert 'Jump:' not in p.view and 'Statistics:' not in p.view
    assert p.title=='Ancient Dragon' and 'Dragons' in p.view and '400' in p.view


def test_directory_navigation_removed_but_curated_links_and_descriptions_remain():
    menu=''.join(f'<a href={i}.cgi>{text}</a> ' for i,text in enumerate(('HOME','ADD A LINK','MODIFY A LINK','NEW LINKS','COOL LINKS','TOP RATED','RANDOM LINK','SEARCH')))
    p=plan('<title>EQ Guilds</title><p>'+menu+'</p><h2>Guilds</h2><p><a href=https://guild.example>Healers of Norrath</a> — a curated raid guild.</p>', 'websites/links.allakhazam.com/20000101000000/Guilds/index.html')
    assert 'MODIFY A LINK' not in p.view and 'Healers of Norrath' in p.view and 'curated raid guild' in p.view


def test_encoded_external_ad_fallback_is_removed_without_erasing_literal_html_in_code():
    ad='&lt;script src="https://external.example/banner.js"&gt;&lt;/script&gt;&lt;noscript&gt;&lt;a href="https://external.example"&gt;&lt;img src="banner.gif"&gt;&lt;/a&gt;&lt;/noscript&gt;'
    p=plan('<title>Guild guide</title><p>'+ad+'</p><article><p>Complete guild instructions.</p><pre><code>'+ad+'</code></pre></article>')
    assert 'encoded_advertising_markup' in p.exclusions
    assert p.view.count('banner.js')==1 and 'Complete guild instructions' in p.view
    assert any(c.kind=='code' and 'banner.js' in c.text for c in p.passages)


@pytest.mark.parametrize('body',[
 '<form><input type=password><textarea></textarea></form>',
 '<form><input name=Username><textarea></textarea></form>',
])
def test_empty_reply_forms_have_no_semantic_content(body):
    p=plan('<title>Necromancer Forums</title>'+body,'websites/forum.example/20000101000000/post.asp')
    assert p.passages==() and 'forum_utility_page' in p.exclusions


def test_populated_reply_quote_or_real_biography_is_not_an_empty_utility():
    p=plan('<title>Reply to guild</title><form><textarea>A surviving quotation from the original message.</textarea><input type=password></form><blockquote>Visible original quotation.</blockquote>', 'websites/forum.example/20000101000000/post.asp')
    assert 'Visible original quotation' in p.view
    article=plan('<title>Profile</title><article>Important guild founder biography.</article>')
    assert 'guild founder biography' in article.view


def test_phpbb_config_error_is_excluded_with_sql_pre_but_a_real_post_about_it_is_retained():
    error='phpBB : Critical Error Could not query config information DEBUG MODE <pre>SELECT config_name FROM config</pre>'
    assert plan(error).passages==()
    assert 'Could not query config information' in plan('<article>'+error+'</article>').view


def test_deep_formatting_is_readable_without_losing_code_images_tables_or_tail():
    raw='<p>Start sentinel</p>'+'<font>'*700+'Deep original testimony'+'</font>'*700+'<pre>if x &lt; 2:\n  return x</pre><img src=raid.png alt=Raid><table><tr><th>Spell</th><th>Mana</th></tr><tr><td>Heal</td><td>400</td></tr></table><p>Tail sentinel</p>'
    text=website_markdown(raw)
    assert all(s in text for s in ('Start sentinel','Deep original testimony','if x < 2','raid.png','Heal','400','Tail sentinel'))


def test_allakhazam_outer_layout_with_a_sidebar_and_headerrow_entity_is_not_navigation():
    raw='<title>Allakhazam</title><table><tr><td><table>'+''.join('<tr><td id=headerrow>'+name+':</td></tr><tr><td><form><select></select></form></td></tr>' for name in ('Bestiary','Jump','Statistics'))+'</table></td><td><table><tr><td id=headerrow>Dragon Scale Breastplate</td></tr><tr><td>Unique item description and AC: 40.</td></tr></table></td></tr></table>'
    p=plan(raw,'websites/everquest.allakhazam.com/20000101000000/db/item.html?item=1')
    assert p.title=='Dragon Scale Breastplate' and 'Unique item description and AC: 40.' in p.view


def test_ubb_missing_cell_closures_keep_author_body_pairing_and_genuine_nested_data():
    p=plan(ubb().replace('Member # 55</td><td>','Member # 55<td>'))
    assert 'ubbclassic_posts' in p.exclusions
    for i in range(2):
        chunks=[c for c in p.passages if f'Firsthand observation {i}' in c.text]
        assert chunks and all(f'Bard{i}' in c.context for c in chunks)
    assert 'Hymn' in p.view and 'Quote 1' in p.view


def skinned_broken_table(missing=False):
    raw='<title>Epic forum</title><table>'
    for i in range(2):
        anchor=f'<a name=post{i+1}></a>' if not missing or i==0 else ''
        raw+=f'''<!-- post #{i+1} --><tr><td>{anchor}November 8th, 2004, 11:33 PM</td><td><div id=postmenu_{i+1}>Speaker{i}</div></td></tr>
        <tr><td colspan=2><!-- message --><p>Unique message {i}.</p><!-- / message --></td></tr>'''
    return raw+'</table>'


def test_vbulletin_numbered_boundaries_bind_each_message_when_post_wrappers_are_missing():
    p=plan(skinned_broken_table())
    assert len(p.passages)==2 and all(c.kind=='post' for c in p.passages)
    assert all(f'Speaker{i}' in c.context and f'Unique message {i}' in c.text for i,c in enumerate(p.passages))


def test_missing_current_vbulletin_anchor_does_not_inherit_a_previous_post_author():
    p=plan(skinned_broken_table(True))
    assert 'Unique message 0' in p.view and 'Unique message 1' in p.view
    assert 'vbulletin_posts' not in p.exclusions


def test_surviving_message_field_is_retained_as_a_quote_without_form_controls():
    raw='<title>Necromancer reply</title><p>Home Search FAQ</p><p>Powered by Snitz Forums</p><form><input name=Username><input type=password><textarea name=Message>[quote]Original complete healing testimony.[/quote]</textarea></form><p>Forum rules footer</p>'
    p=plan(raw,'websites/another-host.example/20000101000000/post.asp')
    assert 'Original complete healing testimony' in p.view
    assert any(c.kind=='quote' for c in p.passages)
    assert 'forum_reply_field_posts' in p.exclusions and 'Forum rules footer' not in p.view


def test_populated_contact_form_on_unrelated_path_keeps_other_unique_content():
    p=plan('<h1>Guild contact details</h1><p>Essential independent guild charter.</p><form><input type=password><textarea>Default message</textarea></form>')
    assert 'Essential independent guild charter' in p.view


def test_ubb_grouped_post_tables_below_author_topic_header_keep_all_messages():
    raw=ubb();start=raw.index('<tr><td><b>Bard0');end=raw.rindex('</table>')
    nested=raw[:start]+'<tr><td colspan=2><table>'+raw[start:end]+'</table></td></tr>'+raw[end:]
    p=plan(nested)
    assert 'ubbclassic_posts' in p.exclusions and 'Firsthand observation 0' in p.view and 'Firsthand observation 1' in p.view
    assert all(f'Bard{i}' in c.context for i in range(2) for c in p.passages if f'Firsthand observation {i}' in c.text)


def test_ubb_header_and_posts_in_sibling_tables_use_the_same_logical_rows():
    raw=ubb().replace('</th></tr><tr><td><b>Bard0','</th></tr></table><table><tr><td><b>Bard0',1)
    p=plan(raw)
    assert 'ubbclassic_posts' in p.exclusions
    assert 'Firsthand observation 0' in p.view and 'Firsthand observation 1' in p.view


def test_allakhazam_catalogue_columns_sidebar_labels_and_comment_subjects_are_not_entity_titles():
    raw='''<title>Illia's Everquest Bestiary</title><h1>Name</h1><table><tr><td id=headerrow>C Net Links:</td></tr></table>
    <table><tr><td id=headerrow>Name</td><td id=headerrow>Level</td></tr><tr><td>Ancient Dragon</td><td>60</td></tr></table>
    <table><tr><td id=headerrow>A comment subject</td><td>By: A Healer</td></tr><tr><td>Posted @ Jan 1 2000</td></tr><tr><td>Useful zone testimony.</td></tr></table>'''
    p=plan(raw,'websites/eqbeastiary.allakhazam.com/20000101000000/search.shtml?zone=1')
    assert p.title=="Illia's Everquest Bestiary"
    assert 'Ancient Dragon' in p.view and 'Useful zone testimony' in p.view
    assert any('A Healer' in c.context for c in p.passages if 'Useful zone testimony' in c.text)
