"""Structural regressions distilled from live captures, without archive fixtures."""
import pytest
from indexer import gemma, passages

READER='The complete unchanged archival reader.'

def plan(raw,host='example.org',path='guide.html'):
    p=passages.prepare(READER,f'websites/{host}/20000101000000/{path}',html_source=raw,mime_type='text/html')
    passages.verify(READER,p)
    assert all(len(gemma.load_tokenizer().encode(x.request).ids)<=480 for x in p.passages)
    return p


def calendar(event=''):
    return '<table id="200606"><tr><td colspan="7"><a href="javascript:__doPostBack(1)">&lt;</a> June 2006 &gt;</td></tr><tr>'+''.join('<td>'+x+'</td>' for x in ('Sun','Mon','Tue','Wed','Thu','Fri','Sat'))+'</tr>'+''.join('<tr>'+''.join(f'<td><a href="javascript:__doPostBack({i})">{i}</a>{event if i==12 else ""}</td>' for i in range(start,start+7))+'</tr>' for start in (1,8,15,22))+'</table>'


def test_generated_calendar_and_login_removed_without_dropping_news_or_events():
    raw='<form id="login"><p>Please login:</p><label>Username:</label><input name="Username"><input type="password"><button>Login</button></form>'+calendar()+'<h1>Guild news</h1><p>We defeated Nagafen.</p>'
    p=plan(raw)
    assert 'Nagafen' in p.view and 'Sun' not in p.view and 'Username' not in p.view
    assert 'calendar_navigation' in p.exclusions and 'login_controls' in p.exclusions
    event=plan(calendar('<a href="/event/raid">Guild raid at 8pm</a>'))
    assert 'Guild raid at 8pm' in event.view


def test_attribution_attaches_to_quote_and_does_not_create_empty_vector():
    raw='<div class="postbody"><h2>Post by Cleric | Jan 1 2000</h2><blockquote>Bring a coffin.</blockquote><p>Agreed.</p></div>'
    p=plan(raw)
    assert not any(x.text.strip().startswith('Post by') and 'Bring a coffin' not in x.text and 'Agreed' not in x.text for x in p.passages)
    assert any(x.kind=='quote' and 'Bring a coffin' in x.text and 'Cleric' in x.request for x in p.passages)
    assert 'Agreed.' in p.view


def test_encoded_host_fallback_parsed_without_embedding_ad_scripts_or_losing_text():
    raw='''<html><frameset><frame src="main.html"></frameset><noframes><body><script src="https://ads.invalid/serve.js"></script><style>.x{color:red}</style><p>Our cleric guide survives without frames.</p></body></noframes></html>'''
    p=plan(raw)
    assert 'cleric guide survives' in p.view
    assert 'script' not in p.view and '&lt;body' not in p.view and 'color:red' not in p.view


@pytest.mark.parametrize('raw',[
 '<h3>Error. Page cannot be displayed. Please contact your service provider for more details. (7)</h3>',
 '<title>Welcome To IIS 4.0!</title><img src="/iissamples/default/IISTitle.gif"><a href="/iissamples/default/LEARN.asp">Learn About</a><a href="/iissamples/default/samples.asp">Samples</a><h1>Welcome to Microsoft® Windows NT® 4.0 Option Pack</h1><p>Internet Information Server. Send comments to iiswish@microsoft.com.</p>',
 '<title>Welcome</title><form><input name="term"></form><h1>TOP 20 SEARCHES</h1>'+''.join(f'<a href="result.php3?term={i}">Loans {i}</a>' for i in range(30))+'<script>window.open("/popup.php")</script>',
])
def test_proven_generated_status_or_parking_page_has_no_semantic_passages(raw):
    p=plan(raw)
    assert not p.passages and p.source_sha256==passages.digest(READER)


def test_eqtraders_navigation_removed_but_recipe_rows_keep_labels_and_trivial():
    menu='<div id="button"><ul>'+''.join(f'<li><a href="/articles/x">{x}</a></li>' for x in ('Home','About','FAQ','Search','Recipes','Trade Supplies','Quests'))+'</ul></div>'
    raw='<title>EQTC:Smithing</title>'+menu+'<h3>Basic Smithing</h3><table><tr><td>Item</td><td>Ingredients</td><td>Trivial</td></tr>'+''.join(f'<tr><td>Recipe {i}</td><td>Ore {i}, water, mold</td><td>{i+20}</td></tr>' for i in range(80))+'</table>'
    p=plan(raw,'www.eqtraders.com','secrets/smithing.html')
    assert 'Home' not in p.view and 'Trade Supplies' not in p.view
    assert p.method=='eqtraders_content'
    for i in range(80):
        assert any(f'Recipe {i} ' in x.text+' ' and f'Ore {i}' in x.text and str(i+20) in x.text for x in p.passages)
    assert all('Ingredients' in x.request and 'Trivial' in x.request for x in p.passages if x.kind=='table')


def test_magelo_detached_headers_bind_to_data_with_character_identity():
    raw='''<title>Magelo profile</title><div id="character"><div id="block1">Arin<br>60 Cleric<br>Tunare</div></div>
    <div id="skill"><div id="skill-header"><table><tr><td>Skill</td><td>Value</td></tr></table></div><div id="skill-data"><table>'''+''.join(f'<tr><td>Skill {i}</td><td>{i+100}</td></tr>' for i in range(100))+'''</table></div><div class="button">Done</div></div>
    <div id="spell"><div id="spell-header"><table><tr><td>Level</td><td>Spell</td></tr></table></div><div id="spell-data"><table><tr><td>60</td><td>Resurrection</td></tr></table></div><div class="button">Done</div></div>
    <div id="bio"><div id="bio-text">I am done with my epic!</div></div>'''
    p=plan(raw,'www.magelo.com','eq_view_profile.html?num=42')
    assert p.method=='magelo_profile' and 'Arin' in p.title and 'Cleric' in p.title
    assert not any(x.text.strip()=='Done' for x in p.passages)
    assert 'I am done with my epic!' in p.view
    skill=[x for x in p.passages if 'Skill 99' in x.text]
    assert skill and all('Arin' in x.request and 'Skill' in x.request and 'Value' in x.request for x in skill)
    assert any('Resurrection' in x.text and 'Spell' in x.request for x in p.passages)


def test_new_magelo_profiles_keep_stats_and_bio_and_drop_empty_panels():
    raw='''<title>Arin - 70 High Elf Cleric [Tunare]</title><div id="profile"><span id="name">Arin</span>
    <div id="playerTabContent"><table id="statTable"><tr><td>HP</td><td>6000</td></tr><tr><td>MANA</td><td>8000</td></tr></table></div>
    <div id="bioTabContent"><div id="bioContent">Guild healer and baker.</div></div>
    <div id="skillOngletContent"><div id="skillOngletButtons">Combat Trade Lang</div><div id="skillContent"></div></div></div>'''
    p=plan(raw,'eq.magelo.com','profile/42/index.html')
    assert p.method=='magelo_profile' and '6000' in p.view and '8000' in p.view and 'Guild healer and baker.' in p.view
    assert 'Combat Trade Lang' not in p.view
    assert all('Arin' in x.request for x in p.passages)


def test_specialized_hosts_and_words_alone_do_not_drop_unrecognized_content():
    raw='<h1>Guild records</h1><p>Calendar navigation troubleshooting: Page cannot be displayed.</p><pre>&lt;script&gt;document.write("ads")&lt;/script&gt;</pre><table><tr><td>Rare recipe</td><td>Water</td></tr></table>'
    for host in ('www.magelo.com','www.eqtraders.com','eq.magelo.com','random.example'):
        p=plan(raw,host)
        assert 'troubleshooting' in p.view and 'document.write' in p.view and 'Rare recipe' in p.view


def test_calendar_only_page_is_empty_but_same_widget_with_an_event_is_content():
    assert not plan(calendar()).passages
    p=plan('<main>'+calendar()+'<p>Schedule reference for our officers.</p></main>')
    assert 'Schedule reference' in p.view
    assert 'Sun' not in p.view  # The inert widget can go; authored prose cannot.


def test_login_wrapper_cannot_remove_an_article_or_populated_message():
    for content in ('<article>Raid results and complete applicant instructions.</article>',
                    '<textarea>A captured reply quoting the raid leader.</textarea>',
                    '<p>'+('Our guild accepts new members. '*20)+'</p>'):
        raw='<form><input name="Username"><input type="password">'+content+'</form>'
        p=plan(raw)
        assert 'Raid results' in p.view or 'captured reply' in p.view or 'guild accepts' in p.view


def test_ad_delivery_panel_is_removed_but_authored_script_examples_and_text_are_safe():
    links=''.join(f'<a href="https://ads.invalid/{i}"><img src="banner{i}.gif" alt="ad {i}"></a>' for i in range(4))
    raw='<div><script src="https://ads.invalid/serve.js"></script>'+links+'</div><p>Our guild guide remains.</p>'
    p=plan(raw)
    assert 'hosting_advertising_panel' in p.exclusions and 'Our guild guide remains.' in p.view and 'ad 1' not in p.view
    for wrapper in ('pre','code','blockquote'):
        p=plan(f'<{wrapper}>&lt;script src="https://ads.invalid/serve.js"&gt;document.write("example")&lt;/script&gt;</{wrapper}>')
        assert 'document.write' in p.view
    p=plan('<div><script src="https://cdn.invalid/library.js"></script>'+links+'<p>A detailed account of our guild raid, written by a player.</p></div>')
    assert 'written by a player' in p.view


def test_expanded_frame_fallback_keeps_authored_navigation_and_only_drops_proven_ad_shell():
    raw='<frameset><frame src="main.html"></frameset><noframes><body><p>A player-written alternate guide.</p><a href="guide.html">Read the guide</a></body></noframes>'
    p=plan(raw)
    assert 'player-written alternate guide' in p.view and 'Read the guide' in p.view
    assert not plan('<frameset><frame src="main.html"></frameset><noframes><body>This page requires frames.</body></noframes>').passages


def test_eqtraders_old_vendor_skill_sections_keep_city_stock_and_location():
    raw='<h3>Qeynos Trade Supplies Locations</h3><p><b>Baking</b></p><ul>'+''.join(f'<li>Flour {i}, mixing bowl - Merchant {i}, South Qeynos LOC 10,20</li>' for i in range(50))+'</ul><p><b>Smithing</b></p><ul><li>Ore, water - Ironforge, North Qeynos LOC 30,40</li></ul>'
    p=plan(raw,'www.eqtraders.com','location/qeynos.html')
    assert p.method=='eqtraders_content'
    assert all('Qeynos' in c.request for c in p.passages)
    assert all('Baking' in c.request and 'South Qeynos' in c.text for c in p.passages if 'Flour' in c.text)
    assert any('Smithing' in c.request and 'Ironforge' in c.text and '30,40' in c.text for c in p.passages)


def test_eqtraders_preserves_described_link_directory_and_recipe_uncertainty():
    raw='<title>EQTC: Links</title><h2>Useful links</h2><ul>'+''.join(f'<li><a href="/site{i}">Home {i}</a> - A detailed independent guide to tradeskills.</li>' for i in range(12))+'</ul><table><tr><th>Item</th><th>Ingredients</th><th>Trivial</th></tr><tr><td>Rare band</td><td>Ore, water; apparently bugged, report a working recipe.</td><td>Unknown</td></tr></table>'
    p=plan(raw,'eqtraders.com','links.html')
    assert p.method=='eqtraders_content' and p.view.count('detailed independent guide')==12
    assert 'apparently bugged' in p.view and 'Unknown' in p.view
    assert 'Rare band' in p.view


def test_unknown_bold_table_data_is_not_promoted_to_column_metadata():
    raw='<title>Glossary</title><table>'+''.join(f'<tr><td><b>Entry {i}</b></td><td><b>Definition {i}</b></td></tr>' for i in range(30))+'</table>'
    p=plan(raw)
    assert all('Entry 0 Definition 0' not in c.context for c in p.passages)
    assert 'Entry 29' in p.view and 'Definition 29' in p.view


def test_unclosed_empty_table_row_cannot_delete_populated_conversion_rows():
    raw='<h3>Clay conversion rates</h3><table><tr bgcolor="grey"><tr><td>3 small blocks clay, flask of water</td><td>block of clay</td></tr><tr><td>1 block of clay, flask of water</td><td>small block of clay</td></tr></table>'
    for host in ('eqtraders.com','unrelated.example'):
        p=plan(raw,host,'secrets/pottery.html')
        assert 'unwrapped_empty_table_row' in p.exclusions
        assert '3 small blocks clay' in p.view and '1 block of clay' in p.view
        assert 'small block of clay' in p.view


def test_eqtraders_pottery_recipe_columns_are_bound_without_assuming_bold_is_a_header():
    raw='<h3>Pottery Secrets</h3><table><tr>'+''.join('<td><b>'+x+'</b></td>' for x in ('item','clay','sketch','extra items','firing sheet(s)'))+'</tr>'+''.join(f'<tr><td>Pot {i}</td><td>large block</td><td>pot sketch</td><td>water, metal bits</td><td>high quality firing sheet</td></tr>' for i in range(60))+'</table>'
    p=plan(raw,'eqtraders.com','secrets/pottery.html')
    assert p.method=='eqtraders_content'
    assert any('Pot 59' in c.text for c in p.passages)
    assert all('clay' in c.context and 'sketch' in c.context and 'firing sheet' in c.context for c in p.passages if c.kind=='table')


def test_profile_empty_modifiers_drop_headers_but_populated_bio_and_zero_stats_survive():
    raw='<title>Magelo profile</title><div id="character"><div id="block1">Arin<br>60 Cleric<br>Tunare</div></div><div id="skill-data"></div><div id="mod"><div id="mod-table-header"><table><tr><td>Item name</td><td>Value</td></tr></table></div><div id="mod-data"></div></div><div id="bio"><div id="bio-text">Done with epic. <a href="bio.html">Full story</a></div></div><table><tr><td>Mana</td><td>0</td></tr></table>'
    p=plan(raw,'www.magelo.com','eq_view_profile.html?num=1')
    assert p.method=='magelo_profile' and 'Item name' not in p.view
    assert 'Done with epic.' in p.view and 'Full story' in p.view and '| 0 |' in p.view
    assert all('Arin' in c.request for c in p.passages)


def test_magelo_specialization_does_not_capture_other_hosts_or_unknown_profile_layouts():
    raw='<h1>Player story</h1><p>Class, skills and spells are important. I am done with the raid.</p>'
    for host,path in (('magelo.example','profile/1/index.html'),('eq.magelo.com','guide.html'),('www.magelo.com','eq_view_profile.html?num=1')):
        p=plan(raw,host,path)
        assert p.method=='html_structure' and 'done with the raid' in p.view


def test_post_header_before_long_quote_keeps_full_quote_and_next_author_separate():
    raw='<h2>Post by Arin | 2000-01-01</h2><blockquote>'+('Quoted recovery instructions. '*800)+'</blockquote><p>Yes.</p><h2>Post by Beth | 2000-01-02</h2><p>No.</p>'
    p=plan(raw)
    assert not any(repr(c.text.strip()).startswith("'Post by Arin") and 'Quoted recovery' not in c.text for c in p.passages)
    assert all('Beth' not in c.request for c in p.passages if 'Quoted recovery' in c.text)
    assert any('Beth' in c.request and 'No.' in c.text for c in p.passages)
    assert 'Yes.' in p.view


def test_feed_attribution_attaches_to_long_subheading_without_crossing_entries():
    raw='''<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Guild news</title>
    <item><title>Raid instructions</title><dc:creator>Arin</dc:creator><pubDate>Thu, 25 May 2006 20:47:49 GMT</pubDate>
    <description><![CDATA[<h3>Healing assignments</h3><p>'''+('Keep the tank alive and preserve mana. '*250)+'''</p>]]></description></item>
    <item><title>Raid instructions</title><dc:creator>Arin</dc:creator><pubDate>Thu, 25 May 2006 20:47:49 GMT</pubDate>
    <description><![CDATA[<h3>Loot policy</h3><p>Roll for the shield.</p>]]></description></item></channel></rss>'''
    p=plan(raw,path='news.rss')
    assert p.method=='feed_entries'
    assert not any(c.text.strip().startswith('Post by') and 'alive' not in c.text and 'shield' not in c.text for c in p.passages)
    assert all('Roll for the shield' not in c.text for c in p.passages if 'alive' in c.text)
    assert any('Arin' in c.request and 'shield' in c.text for c in p.passages)


def test_search_ad_template_requires_whole_page_evidence_not_just_link_words():
    links=''.join(f'<a href="result.php3?term={i}">Category {i}</a>' for i in range(12))
    ads=''.join(f'<a href="https://ads.invalid/findwhat.dll?clickthrough={i}">A sponsored listing {i}</a>' for i in range(6))
    template='<title>Welcome</title><form><select name="term"><option>Search</option></select></form><h1>TOP 20 SEARCHES</h1>'+links+ads+(', '*220)
    p=plan(template)
    assert p.method=='archive_status' and not p.passages and 'archived_parking_page' in p.exclusions
    authored='<p>'+('A tradeskill guide written by a player with complete equipment recommendations. '*10)+'</p>'
    p=plan(template+authored)
    assert 'tradeskill guide' in p.view and p.passages
    assert plan(template.replace(ads,'')).passages
    p=plan('<title>Welcome to Microsoft Windows NT 4.0</title><h1>Debugging the webserver</h1><p>Internet Information Server Samples fail to load. Our solution is to restart the service.</p>')
    assert 'Our solution' in p.view and p.passages


def test_ad_only_frame_fallback_is_status_but_download_library_does_not_prove_ads():
    links=''.join(f'<a href="page{i}.html"><img src="image{i}.gif" alt="Page {i}"></a>' for i in range(4))
    panel='<div><script src="https://ads.invalid/serve.js"></script>'+links+'</div>'
    p=plan('<frameset><frame src="guild.html"></frameset><noframes><body>'+panel+'</body></noframes>')
    assert not p.passages and 'advertising_frames_shell' in p.exclusions
    p=plan(panel.replace('https://ads.invalid/serve.js','https://cdn.invalid/download.js'))
    assert 'Page 0' in p.view and 'hosting_advertising_panel' not in p.exclusions


def test_eqtraders_legacy_buttons_and_exact_footer_do_not_remove_authored_copyright():
    buttons='<title>EQ Traders Corner</title>'+''.join(f'<a href="guide{i}.html">Guide {i}</a> ' for i in range(12))
    p=plan(buttons,'eqtraders.com','buttons.html')
    assert 'eqtraders_navigation' in p.exclusions and 'Guide 0' not in p.view
    raw='<title>EQTC Guide</title><img alt="EQ Traders Corner"><img alt="Racknine"><p>All information, articles and graphics on this site are the copyright of EQ Traders Corner.</p><p>My copyrighted guide to ore.</p>'
    p=plan(raw,'eqtraders.com','guide.html')
    assert 'eqtraders_footer' in p.exclusions and 'My copyrighted guide' in p.view


def test_magelo_empty_stats_and_unknown_tab_content_are_conservative():
    old='<div id="character"><div id="block1">Arin 60 Cleric Tunare</div></div><div id="skill-data"></div><div id="block2"><table><tr><td>HP</td><td></td></tr><tr><td>Mana</td><td>0</td></tr></table></div>'
    p=plan(old,'magelo.com','eq_view_profile.html?num=1')
    assert 'HP' not in p.view and 'Mana' in p.view and '| 0 |' in p.view
    modern='<div id="profile"><div id="name">Arin</div><table id="statTable"><tr><td>HP</td><td>4000</td></tr></table><div id="bioTabContent"><div id="bioContent"></div><p>Unexpected authored biography outside the normal data div.</p></div><div id="skillContent">Complete captured skills.</div></div>'
    p=plan(modern,'eq.magelo.com','profile/1/index.html')
    assert 'Unexpected authored biography' in p.view and 'Complete captured skills' in p.view
    assert 'Arin' in p.title and all('Arin' in c.request for c in p.passages)
