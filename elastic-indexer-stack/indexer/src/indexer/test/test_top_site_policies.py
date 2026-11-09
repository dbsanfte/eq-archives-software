"""Cross-host structural regressions from the fifty-site source audit."""
import pytest
from indexer import passages, gemma


def plan(raw, host='forum.example', path='topic.html'):
    reader='Complete archival reader remains unchanged.'
    result=passages.prepare(reader,f'websites/{host}/20000101000000/{path}',
                            html_source=raw,mime_type='text/html')
    passages.verify(reader,result)
    assert all(len(gemma.load_tokenizer().encode(p.request).ids)<=480 for p in result.passages)
    return result


def ezpost(author,body):
    return f'<tr><td><a href="bboard.showUserPublicProfile?gid={author}">{author}</a> Member (9/4/03 5:00 pm)</td><td>{body}</td></tr>'


def test_ezboard_inline_ad_does_not_destroy_message_boundaries():
    ad='<tr><td colspan="3"><iframe src="http://ads.ezboard.com/oasis/ad.php">&lt;A HREF="http://ads.ezboard.com/click"&gt;&lt;IMG SRC="ad.gif"&gt;&lt;/A&gt;</iframe></td></tr>'
    raw='<table><tr><td>Author</td><td>Comment</td></tr>'+ezpost('Arin','First raid report.')+ad+ezpost('Bela','<blockquote>First raid report.</blockquote>Second raid report.<table><tr><th>Item</th><th>Value</th></tr><tr><td>Shield</td><td>42</td></tr></table>')+'</table>'
    p=plan(raw,'pub6.ezboard.com','fboard.showMessageRange?topicID=3.topic')
    assert p.method=='forum_posts'
    assert 'ads.ezboard.com' not in p.view
    assert 'Arin' in p.view and 'Bela' in p.view and 'Shield' in p.view and '42' in p.view
    assert any('Second raid report.' in x.text and 'Bela' in x.request for x in p.passages)
    assert not any('First raid report.' in x.text and 'Second raid report.' in x.text and x.kind!='quote' for x in p.passages)


def test_ezboard_unknown_interstitial_is_preserved_instead_of_silently_dropped():
    raw='<table><tr><td>Author</td><td>Comment</td></tr>'+ezpost('Arin','First post.')+'<tr><td colspan="2">An independent captured announcement.</td></tr>'+ezpost('Bela','Second post.')+'</table>'
    p=plan(raw,'pub6.ezboard.com')
    assert all(x in p.view for x in ('First post.','independent captured announcement','Second post.'))


def test_empty_ezboard_composer_is_utility_but_populated_quote_survives():
    raw='<title>New Topic - www.ezboard.com</title><form action="fboard.addTopic"><input name="ezUser"><input type="password"><input name="subject"><textarea name="comment">{}</textarea><p>Formatting: Plain Text ezCodes HTML</p></form>'
    assert not plan(raw.format(''),'pub6.ezboard.com','fboard.showAddTopicScreenFromWeb').passages
    p=plan(raw.format('Bring resist gear and the raid map.'),'pub6.ezboard.com','fboard.showAddTopicScreenFromWeb')
    assert 'Bring resist gear and the raid map.' in p.view


@pytest.mark.parametrize('host',['www.everlore.com','eq.castersrealm.com','unrelated-forum.example'])
def test_legacy_ubb_nested_rule_preserves_each_authored_post(host):
    header='<tr><td><b>Author</b></td><td><b>Topic: Bard or Druid?</b></td></tr>'
    def post(name,body):
        return f'<tr><td><b>{name}</b><br>unregistered</td><td><font><font>posted 10-11-1999 06:02 AM <a href="/cgi-bin/ubbmisc.cgi?action=getbio">Profile</a></font><hr>{body}</font><p align="right">IP: <a href="/cgi-bin/ubbmisc.cgi?action=getip">Logged</a></p></td></tr>'
    raw='<table>'+header+post('Arin','Bards get songs.<blockquote>A real quoted tip.</blockquote>')+post('Bela','Druids get wolf form.<a href="map.zip">Download map</a>')+'</table>'
    p=plan(raw,host)
    assert p.method=='forum_posts'
    assert 'Bard or Druid?' in p.title
    assert 'IP:' not in p.view and 'Logged' not in p.view
    assert all(x in p.view for x in ('Bards get songs.','A real quoted tip.','Druids get wolf form.','Download map'))
    assert any('Druids get wolf form.' in x.text and 'Bela' in x.request for x in p.passages)
    assert not any('Bards get songs.' in x.text and 'Druids get wolf form.' in x.text for x in p.passages)


def test_nested_script_in_javascript_string_does_not_leak_host_ad_code():
    raw='''<html><body><script><!--
var i=1;
document.write('<iframe src="http://ads.example/banner"><script src="http://ads.example/ad.js"></script><noscript><img src="ad.gif"></noscript></iframe>');
if(i){ document.write('Click here to visit our sponsor'); }
//--></script><h1>Japanese guild guide</h1><p>攻略と呪文の説明。Bring a coffin.</p></body></html>'''
    p=plan(raw,'random-host.example')
    assert '攻略と呪文の説明' in p.view and 'Bring a coffin.' in p.view
    assert 'document.write' not in p.view and 'var i' not in p.view and 'visit our sponsor' not in p.view
    # Authored explanations and uncertain scripts cannot consume the article.
    p=plan('<script>var x="broken;</script><h1>Script troubleshooting</h1><p>Use document.write carefully.</p><script>var y=1;</script>')
    assert 'Script troubleshooting' in p.view and 'document.write carefully' in p.view


@pytest.mark.parametrize('host',['www.eqinterface.com','www.eqrankings.com','maximumeq.com','www.eqdiva.com'])
def test_catalog_column_labels_follow_every_continuation(host):
    raw='<title>Archived equipment list</title><table><tr><td><b>Name</b></td><td><b>AC</b></td><td><b>HP</b></td></tr>'+''.join(f'<tr><td>Shield {i}</td><td>{i+10}</td><td>{i+100}</td></tr>' for i in range(90))+'</table>'
    p=plan(raw,host)
    assert 'catalog_column_headers' in p.exclusions
    assert all('Name' in x.context and 'AC' in x.context and 'HP' in x.context for x in p.passages if x.kind=='table')
    assert all(f'Shield {i} ' in p.view+' ' for i in range(90))


def test_eqinterface_menu_removed_without_losing_authored_downloads_or_code():
    labels=('Home','Forums','FAQ','Downloads [+]','Featured!','Complete Sets','Mac Interfaces','Group window')
    menu='<table><tr><td><b>Menu</b></td></tr><tr><td>'+''.join(f'<a href="/menu/{i}">{v}</a><br>' for i,v in enumerate(labels))+'</td></tr></table>'
    raw='<title>EQInterface</title>'+menu+'<h1>Resurrection window</h1><p>Restores the classic UI.</p><pre>function draw_window() { return 42; }</pre><a href="ui.zip">Download UI</a>'
    p=plan(raw,'www.eqinterface.com')
    assert 'eqinterface_category_menu' in p.exclusions and 'Complete Sets' not in p.view
    assert all(v in p.view for v in ('Resurrection window','Restores the classic UI','draw_window','Download UI'))
    # A named authored directory on an unrelated site keeps its links.
    assert 'Complete Sets' in plan(raw,'guild.example').view


def test_game_network_bar_removed_but_described_or_quoted_directories_survive():
    menu='<table><tr>'+''.join(f'<td><a href="https://game{i}.example">{v}</a></td>' for i,v in enumerate(('EQ','EQ II','DAoC','MTG','AC','AC II','AO','SWG','SB')))+'</tr></table>'
    p=plan(menu+'<h1>Quest guide</h1><p>Bring a ruby.</p>','www.everlore.com')
    assert 'game_network_navigation' in p.exclusions and 'DAoC' not in p.view and 'Bring a ruby' in p.view
    for raw in ('<blockquote>'+menu+'</blockquote>',menu.replace('</table>','<tr><td>A player-written guide to related games.</td></tr></table>')):
        p=plan(raw,'www.everlore.com');assert 'DAoC' in p.view


def test_snowboards_rows_keep_authors_dates_and_message_content():
    def post(name,body):
        return f'<tr><td rowspan="3"><a class="AuthorLink" href="/user.asp?usr=42">{name}</a>Posts: 99</td><td class="BoardRowA">Date Posted: 3/9 7:57pm Subject: Raid plans</td></tr><tr><td class="BoardRowB" colspan="2">{body}</td></tr><tr><td><a href="/post.asp?brd=5">Post Reply</a><a href="/user_posts.asp?usr=42">Post History</a></td></tr>'
    p=plan('<table>'+post('Arin','Bring cold resist gear.')+post('Bela','<blockquote>Bring cold resist gear.</blockquote>I have a spare shield.')+'</table>','vnboards.ign.com')
    assert p.method=='forum_posts' and 'Raid plans' in p.title
    assert 'Post History' not in p.view and 'Posts: 99' not in p.view
    assert any('spare shield' in x.text and 'Bela' in x.request and '3/9 7:57pm' in x.request for x in p.passages)


def test_magelo_forum_messages_are_separate_from_profile_metadata():
    def post(name,body):
        return f'<tr><td class="FORUM-SUB-CEL-LEFT"><span class="WINDOW-BODY-TITLE">{name}</span>Premium User Nbr post: 20</td><td class="FORUM-MAIN-CEL"><table><tr><td>Posted: 10/12/08 11:00 AM</td></tr><tr><td class="FORUM-MAIN-CEL-BODY">{body}</td></tr></table></td></tr>'
    p=plan('<h2>Messages in topic: Character updates</h2><table>'+post('Arin','Sync cannot detect my character.')+post('Bela','We are investigating.<a href="fix.zip">Attachment</a>')+'</table>','eq.magelo.com','forum/messages.jspa?topic=42')
    assert p.method=='forum_posts'
    assert 'Premium User' not in p.view and 'Attachment' in p.view
    assert any('investigating' in x.text and 'Bela' in x.request for x in p.passages)


def test_phpbb_one_classless_rows_bind_iso_date_and_each_body():
    def post(name,body):
        return f'<tr><td><b>{name}</b>Joined: Mar 11, 2002 Posts: 54</td><td><font>Posted: 2002-05-15 10:21</font><hr>{body}<hr><a href="/Forums/bb_profile.php?mode=view&user=4">View profile</a><a href="/Forums/editpost.php?post_id=99">Edit</a></td></tr>'
    raw='<table>'+post('Arin','First news report.<pre>keep_code();</pre>')+post('Bela','Second news report.')+'</table>'
    p=plan(raw,'unrelated-guild.example','Forums/viewtopic.php?topic=42')
    assert p.method=='forum_posts' and 'keep_code' in p.view
    assert any('Second news report' in x.text and 'Bela' in x.request and '2002-05-15 10:21' in x.request for x in p.passages)


def test_ubbthreads_font_message_retains_subject_and_own_post_date():
    raw='<table><tr><td rowspan="2"><a name="Post42"></a><a href="showprofile.php?User=Arin">Arin</a><span class="small">Reged: 01/01/99</span></td><td class="subjecttable"><b>Title Changes</b><font class="small">06/19/02 06:54 PM</font></td></tr><tr><td><font class="post">Guild titles now work.<font class="small">Post Extras: <a href="printthread.php">Print Post</a></font></font></td></tr></table>'
    p=plan(raw,'www.ralloszek.net')
    assert p.method=='forum_posts' and 'Guild titles now work.' in p.view
    assert 'Post Extras' not in p.view
    assert any('Arin' in x.request and '06/19/02 06:54 PM' in x.request for x in p.passages)


def test_major_catalog_entities_come_from_source_not_the_url():
    raw='<title>Search Results</title><b>Lizard Man</b><p>A faction in Feerrott.</p><b>Discuss Lizard Man</b><p>'+('A genuine captured faction comment. '*100)+'</p>'
    p=plan(raw,'www.eqspoilers.com','cgi-bin/webdata_pro.pl?_layout=factionfull&name=wrong')
    assert p.title=='Lizard Man' and all('Lizard Man' in x.request for x in p.passages)
    p=plan('<title>View Item Comments</title><table><tr><td>Stormreaper Boned Cloak<br>Stats: AC: 11 Wt: 3 Slot: back<br>Attributes: WIS+10 HP+40</td></tr></table><p>'+('A captured item comment. '*100)+'</p>','www.eqprices.com','scripts/showcomments.asp?itemcode=wrong')
    assert p.title=='Stormreaper Boned Cloak' and all('Stormreaper Boned Cloak' in x.request for x in p.passages)
    p=plan('<title>Search Results</title><b>Another entity</b><b>Discuss Lizard Man</b>','www.eqspoilers.com')
    assert p.title!='Lizard Man'


@pytest.mark.parametrize('days',[('Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'),('Su','Mo','Tu','We','Th','Fr','Sa')])
def test_calendar_variants_remove_only_empty_day_grids(days):
    raw='<table><tr>'+''.join('<th>'+day+'</th>' for day in days)+'</tr>'+''.join('<tr>'+''.join('<td>'+str(i)+'</td>' for i in range(start,start+7))+'</tr>' for start in (1,8,15,22))+'</table><p>Guild raid report.</p>'
    assert not any(x in plan(raw).view for x in days)
    assert 'Guild raid report' in plan(raw).view
    assert 'Vox raid tonight' in plan(raw.replace('<td>12</td>','<td>12 Vox raid tonight</td>')).view


@pytest.mark.parametrize('message',['No Forum specified.','No Post specified.','Invalid Attachment specified.'])
def test_vbulletin_generated_missing_entities_clear_passages_but_authored_examples_survive(message):
    raw='<h2>vBulletin Message</h2><p>'+message+' If you followed a valid link, please notify the webmaster.</p>'
    assert not plan(raw).passages
    assert message in plan('<article>'+raw+'<p>Here is how to repair it.</p></article>').view


def test_graffe_spell_data_keeps_spell_identity_on_raw_field_continuations():
    def spell(number,name):
        return f'<table class="spell"><tr><td class="spellhead3"><a title="Click here for more spell information">{name}</a></td><td><div id="spellabstract{number}">Cold damage</div></td></tr></table><div id="spelldata{number}">'+''.join(f'<p>rawfield{i}: {number}</p>' for i in range(100))+'</div>'
    raw='<title>Wizard spells</title>'+spell(42,'Ice Comet')+spell(43,'Frost Bolt')
    p=plan(raw,'www.graffe.com','spells.php?level=49')
    assert p.method=='spell_catalog'
    assert all('Ice Comet' in x.request for x in p.passages if 'rawfield99: 42' in x.text)
    assert all('Frost Bolt' in x.request for x in p.passages if 'rawfield99: 43' in x.text)
    assert not any('Ice Comet' in x.text and 'Frost Bolt' in x.text for x in p.passages)
    assert all(f'rawfield{i}: {n}' in p.view for i in range(100) for n in (42,43))


def test_everquest_online_item_list_does_not_merge_distinct_items():
    def item(number,name):
        return f'<li><a href="/items/shield-id{number}.php" title="{name}"></a>{name}<span><b>Stats:</b>AC:42 HP:100<br><b>Class:</b>CLR<br><b>Race:</b>ALL</span></li>'
    p=plan('<h1>Armor</h1><ul>'+item(1,'Shield of Dawn')+item(2,'Shield of Dusk')+'</ul>','www.everquest-online.com')
    assert p.method=='item_catalog'
    assert not any('Shield of Dawn' in x.text and 'Shield of Dusk' in x.text for x in p.passages)
    assert all('Shield of Dawn' in x.request for x in p.passages if 'Shield of Dawn' in x.text)
    assert p.view.count('AC:42 HP:100')==2


def test_eqdiva_resist_gear_keeps_slot_and_all_column_labels():
    def panel(slot):
        return f'<tr><td colspan="8" bgcolor="#9999cc"><b>{slot}</b></td></tr><tr><td></td>'+''.join('<td><b>'+label+'</b></td>' for label in ('MR','FR','CR','PR','DR','AC','Bard Only'))+'</tr>'+''.join('<tr><td>'+slot+' item '+str(i)+'</td>'+''.join('<td>'+str(v)+'</td>' for v in (1,2,3,4,5,6,0))+'</tr>' for i in range(40))
    p=plan('<table>'+panel('HEAD')+panel('CHEST')+'</table><p>Player-written recommendations remain.</p>','www.eqdiva.com','items/resistGear.asp?id=1')
    assert 'eqdiva_armor_sections' in p.exclusions and 'Player-written recommendations remain.' in p.view
    assert all('HEAD' in x.request and 'MR' in x.request and 'Bard Only' in x.request for x in p.passages if 'HEAD item' in x.text)
    assert all('CHEST' in x.request and 'MR' in x.request for x in p.passages if 'CHEST item' in x.text)
    p=plan('<td><b>Bard Song Locations</b><br><b>Kelethin</b><br>Continent: Faydwer<blockquote>Located in the treetops.</blockquote></td>','www.eqdiva.com','locations.asp?loc=Wrong')
    assert 'Kelethin' in p.title and 'Located in the treetops.' in p.view


def test_vbulletin_missing_message_close_can_use_its_own_proven_body_cell():
    raw='<table>'
    for i in range(2):
        raw+=f'<tr><td><a name="post{i+1}"></a>06-26-2002, 07:23 PM</td><td>Thread header</td></tr><tr><td><b>Speaker{i}</b>Joined: Mar 2002</td><td><!-- message --><p>Unique message {i}.</p>'+('<!-- / message -->' if i==0 else '')+'</td></tr>'
    raw+='</table>'
    p=plan(raw)
    assert p.method=='forum_posts'
    assert all(any(f'Unique message {i}.' in x.text and f'Speaker{i}' in x.request for x in p.passages) for i in range(2))
    # A truly uncertain author/boundary keeps the complete generic document.
    p=plan(raw.replace('<a name="post2"></a>',''))
    assert 'Unique message 1.' in p.view


def test_guildmagic_guild_and_server_follow_every_statistics_chunk():
    header='<table><tr><td><div class="rosterguildname">Hand of Dawn</div></td><td><a href="browse_server.cfm?id=30">Rallos Zek</a><a href="guild_stats.cfm?ID=42">View stats</a><a href="guild_characters.cfm?ID=42">View roster</a></td></tr></table>'
    body=''.join(f'<p>Captured statistic {i}: 0 warriors, 2 clerics, 6 characters.</p>' for i in range(100))
    p=plan('<title>Guild Magic</title>'+header+body,'eq.guildmagic.com','guild_stats.cfm?ID=42')
    assert p.method=='guildmagic_roster'
    assert all('Hand of Dawn' in x.request and 'Rallos Zek' in x.request for x in p.passages)
    assert all(f'Captured statistic {i}: 0 warriors' in p.view for i in range(100))
    for raw in (header.replace('guild_characters.cfm?ID=42','guild_characters.cfm?ID=99')+body,
                header+'<div class="rosterguildname">Another guild</div>'+body):
        p=plan('<title>Guild Magic</title>'+raw,'eq.guildmagic.com')
        assert p.method!='guildmagic_roster' and 'Captured statistic 99' in p.view
