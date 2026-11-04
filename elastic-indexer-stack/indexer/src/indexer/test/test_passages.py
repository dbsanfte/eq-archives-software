from dataclasses import replace

import pytest

from indexer import gemma, passages

RID = "websites/example.org/20000101000000/guide.html"


def plan(raw, rid=RID, title="", mime="text/html"):
    return passages.prepare("The complete archival reader, including its navigation.",rid,title,
                            html_source=raw,mime_type=mime)


def assert_valid(result):
    passages.verify("The complete archival reader, including its navigation.",result)
    assert all(len(gemma.load_tokenizer().encode(p.request).ids)<=480 for p in result.passages)
    assert result.provenance()["view_sha256"]==passages.digest(result.view)


def test_unknown_guild_layout_keeps_unique_sidebar_text_and_link_directory():
    raw='''<html><title>The Lantern Guild</title><body><nav>Login Search</nav>
    <aside class="sidebar">Our raid starts at the waterfall every Tuesday. Bring cold resistance.</aside>
    <h1>The Lantern Guild</h1><p>New applicants should contact our cleric.</p>
    <p><a href="/epic.html">Cleric epic guide</a> <a href="/faction.html">Faction reference</a></p>
    </body></html>'''
    result=plan(raw)
    assert "waterfall every Tuesday" in result.view
    assert "Cleric epic guide" in result.view and "Faction reference" in result.view
    assert "Login Search" not in result.view
    assert "http" not in result.view
    assert_valid(result)


def test_main_landmark_and_ambiguous_or_tiny_articles_have_safe_fallbacks():
    article="Our clerics recovered the warrior's corpse in Lower Guk. "*12
    result=plan('<body><nav>Account controls</nav><main><h1>Corpse recovery</h1><p>'+article+'</p></main></body>')
    assert article.strip() in result.view and "Account controls" not in result.view
    assert "outside_main_landmark" in result.exclusions
    for raw in ('<article>Short note</article><p>Essential guild instructions</p>',
                '<article>First story</article><article>Second story</article>'):
        result=plan(raw)
        assert "outside_main_landmark" not in result.exclusions
        assert "instructions" in result.view or "Second story" in result.view
        assert_valid(result)


def test_main_landmark_does_not_discard_comments_or_unique_prose_outside_it():
    raw='<article><h1>Cleric guide</h1><p>'+('Healing advice. '*30)+'</p></article><div>Comment by Arin: This spell also restores stamina.</div>'
    result=plan(raw)
    assert 'This spell also restores stamina' in result.view
    assert 'Healing advice' in result.view
    assert 'outside_main_landmark' not in result.exclusions
    assert_valid(result)


def test_old_link_directory_is_retained_when_navigation_is_the_entire_page():
    raw='<html><title>Cleric research directory</title><nav>'+''.join(
        f'<a href="/guide{i}.html">Independent resurrection and healing guide number {i}</a>' for i in range(12))+'</nav></html>'
    result=plan(raw)
    assert 'conservative_layout_fallback' in result.exclusions
    assert all(f'guide number {i}' in result.view for i in range(12))
    assert result.passages
    assert_valid(result)


@pytest.mark.parametrize('host', ['everquest.allakhazam.com','www.everlore.com','eq.castersrealm.com'])
def test_aggregator_sidebar_removed_but_entity_data_and_curated_tables_retained(host):
    labels=['News','Search','Contact Us','Login','Links','Main','Zones','Spells','Maps']
    nav='<table>'+''.join('<tr><td><a href="/menu">'+label+'</a></td></tr>' for label in labels)+'</table>'
    data='<table><tr><td id="headerrow">Lantern of Dawn</td></tr><tr><td>AC: 12; Wisdom: 9</td></tr></table>'
    result=plan('<html><title>Generic brand</title><body>'+nav+data+'</body></html>',
                'websites/'+host+'/20000101000000/item.html')
    assert "Lantern of Dawn" in result.view and "Wisdom: 9" in result.view
    assert "legacy_aggregator_navigation" in result.exclusions
    assert "Contact Us" not in result.view
    assert_valid(result)


def test_content_marker_and_link_heavy_data_table_are_not_mistaken_for_navigation():
    raw='''<html><title>Baking</title><body><p>Unrelated menu</p><!--#beginContent#-->
    <h1>Baking recipes</h1><table><tr><th>Recipe</th><th>Ingredients</th></tr>'''+''.join(
    f'<tr><td><a href="/{i}">Recipe {i}</a></td><td>Meat, bread</td></tr>' for i in range(100))+'''
    </table><!--#endContent#--><p>Unrelated footer</p></body></html>'''
    result=plan(raw)
    assert 'outside_explicit_content_markers' in result.exclusions
    assert 'Unrelated' not in result.view
    assert all(f'Recipe {i}' in result.view for i in range(100))
    table=[p for p in result.passages if p.kind=='table']
    assert len(table)>2
    assert all('Ingredients' in p.context for p in table)
    assert all(p.text.lstrip().startswith('|') for p in table)
    assert_valid(result)


def test_normal_table_rows_stay_whole_and_oversized_cells_still_have_full_coverage():
    raw='<body><table><tr><th>Item</th><th>Effect</th></tr>'+''.join(
        f'<tr><td>Item {i}</td><td>'+('Resurrection and healing. '*8)+'</td></tr>' for i in range(25))+'</table></body>'
    result=plan(raw)
    table=[p for p in result.passages if p.kind=='table']
    assert len(table)>2
    assert all(p.text.endswith('\n') for p in table)
    assert all('Table columns:' in p.context for p in table)
    for p in table:
        assert not p.text.startswith('and healing')
    result=plan('<table><tr><th>Item</th><th>Lore</th></tr><tr><td>Epic</td><td>'+('Lore 🧝 聖職者 '*900)+'</td></tr></table>')
    assert len(result.passages)>3
    assert_valid(result)


@pytest.mark.parametrize('collection', ['mailing-lists/clerics/1.json','newsgroups/eq/1.txt'])
def test_message_headers_become_context_and_short_messages_stay_one_piece(collection):
    source='<h1>Subject: Resurrection spell</h1>\n\n### From: A Cleric\n\n### Date: 2000-01-01\n\n---\n\nBring a coffin to the raid.'
    result=passages.prepare(source,collection)
    assert result.title=='Resurrection spell'
    assert len(result.passages)==1
    assert 'From: A Cleric' in result.passages[0].context
    assert result.passages[0].text.strip()=='Bring a coffin to the raid.'
    passages.verify(source,result)


def test_quoted_conversation_survives_an_ad_appended_without_blank_lines():
    source='# Subject: Recovery\n\n---\n\nNew reply.\n> Quoted original testimony.\n> More important evidence.\n> ONElist is THE place where the world talks!\n> Additional original evidence after the ad.'
    result=passages.prepare(source,'mailing-lists/warriors/2.json')
    assert 'Quoted original testimony.' in result.view
    assert 'More important evidence.' in result.view
    assert 'Additional original evidence after the ad.' in result.view
    assert 'ONElist is THE place' not in result.view
    assert len(result.passages)==2
    assert 'Quoted correspondence' in result.passages[1].context
    passages.verify(source,result)


def test_long_wrapped_quotes_do_not_create_a_fragment_for_every_line():
    source='# Subject: Long reply\n\n---\n\n'+('> Important quoted evidence\nwrapped continuation line.\n'*100)
    result=passages.prepare(source,'newsgroups/eq/3.txt')
    assert 2<len(result.passages)<12
    assert all('Quoted correspondence:' in p.context for p in result.passages)
    passages.verify(source,result)


def test_rss_entries_have_independent_title_date_and_body_boundaries():
    raw='<rss><channel><title>EQ News</title>'+''.join(
        f'<item><title>Patch {i}</title><pubDate>2000-01-{i+1:02}</pubDate><description>Cleric spell update {i}.</description></item>' for i in range(3))+'</channel></rss>'
    result=plan(raw,mime='text/xml')
    assert result.method=='feed_entries'
    assert len(result.passages)==3
    for i,p in enumerate(result.passages):
        assert f'Patch {i}' in p.request and f'update {i}' in p.text
        assert f'update {(i+1)%3}' not in p.text
    assert_valid(result)


def test_ezboard_board_directory_keeps_forum_descriptions_and_post_bodies():
    raw='''<html><title>Clerics @ ezboard</title><nav>Register Login</nav>
    <p>Our guild welcomes new clerics.</p><table><tr><th>Forum</th><th>Description</th></tr>
    <tr><td><a href="fclerics1">Resurrection</a></td><td>Corpse recovery advice.</td></tr></table>
    <div class="postbody">Bring peridots to the next raid.</div></html>'''
    result=plan(raw,'websites/pub1.ezboard.com/20000101000000/bclerics/index.html')
    assert 'Corpse recovery advice.' in result.view
    assert 'Bring peridots' in result.view and 'Our guild welcomes' in result.view
    assert 'Register Login' not in result.view
    assert_valid(result)


def test_ezboard_thread_retains_separate_authors_dates_and_comments():
    raw='<html><title>Cleric epic help</title><table><tr><td>Author</td><td>Comment</td></tr>'+''.join(
        f'<tr><td><a href="profile">Cleric{i}</a> Member Posts: 20 (1/{i+1}/2000 2:30 pm)</td><td>Distinct testimony {i}.</td></tr>'
        for i in range(3))+'</table></html>'
    result=plan(raw,'websites/pub1.ezboard.com/20000101000000/fclerics.showMessage?topicID=1')
    assert len(result.passages)==3
    assert 'ezboard_author_comment_structure' in result.exclusions
    for i,p in enumerate(result.passages):
        assert f'Cleric{i}' in p.request and f'Distinct testimony {i}' in p.text
        assert 'Posts: 20' not in p.request
    assert_valid(result)


def test_allakhazam_item_and_comments_have_separate_source_derived_boundaries():
    raw='<title>Allakhazam</title><table><tr><td id="headerrow">A real item</td></tr><tr><td>AC: 10</td></tr></table>'
    for i in range(3):
        raw+=f'<table><tr><td id="headerrow">Subject {i}</td><td id="headerrow">By: Author{i}</td></tr><tr><td id="headerrow">Posted @ Jan {i+1} 2000</td></tr><tr><td colspan=2>Unique testimony {i}.</td></tr><tr><td>Top</td></tr></table>'
    result=plan(raw,'websites/everquest.allakhazam.com/20000101000000/db/item.html?item=1')
    assert result.title=='A real item' and 'AC: 10' in result.view
    assert 'allakhazam_comment_controls' in result.exclusions
    posts=[p for p in result.passages if 'Unique testimony' in p.text]
    assert len(posts)==3
    for i,p in enumerate(posts):
        assert f'Author{i}' in p.request and f'Jan {i+1} 2000' in p.request
    assert_valid(result)


def test_archived_error_page_does_not_erase_a_real_article_about_errors():
    excluded=plan('<title>System Message: Error</title><p>This forum is unavailable.</p>')
    assert excluded.passages==() and 'archived_access_or_error_page' in excluded.exclusions
    article=plan('<title>404 Not Found</title><article><p>'+('Our guild guide explains how to recover a missing webpage. '*5)+'</p></article>')
    assert article.passages and 'guild guide' in article.view


@pytest.mark.parametrize('path',['ads.js','style.css','robots.txt','font.woff2'])
def test_supporting_assets_remain_reader_sources_without_semantic_passages(path):
    source='Original complete supporting file'
    result=passages.prepare(source,'websites/example.org/20000101000000/'+path)
    assert result.view=='' and result.passages==()
    assert 'supporting_asset' in result.exclusions
    assert result.source_sha256==passages.digest(source)
    passages.verify(source,result)


def test_mime_alone_does_not_drop_a_misclassified_newsgroup_post():
    source='A real account of resurrecting a fallen guild member.'
    result=passages.prepare(source,'newsgroups/eq/post.txt',mime_type='text/x-c')
    assert source in result.view
    assert result.passages


@pytest.mark.parametrize('value',[None,'','   ','[ Still awaiting LLM Enrichment... ]','Still awaiting LLM Enrichment'])
def test_placeholder_summaries_never_use_an_inference_request(value):
    assert passages.useful_summary(value) is None


def test_real_summaries_and_ocr_lists_remain_eligible():
    assert passages.useful_summary('The post discusses awaiting a resurrection.')
    assert passages.useful_summary(['Cleric','raid'])=='Cleric raid'
    assert passages.identity('websites/www.eq.org/20000101000000/a.js?q=1')==('www.eq.org','/a.js')
    assert passages.identity('mailing-lists/eq/1.json')[0]==''


def test_stale_missing_changed_and_oversized_prepared_passages_are_rejected():
    source='Complete source. '*900
    result=passages.prepare(source)
    for bad in (replace(result,source_sha256='stale'),replace(result,version='old'),
                replace(result,passages=()),replace(result,passages=(replace(result.passages[0],text='changed'),)),
                replace(result,passages=(replace(result.passages[0],context='too much '*1000),))):
        with pytest.raises(ValueError):passages.verify(source,bad)
    assert passages.prepare('').passages==()
    assert_valid(plan('<body><img src="spacer.gif"></body>'))


def test_long_titles_and_unicode_sources_count_every_context_token():
    result=passages.prepare('聖職者 resurrection 🧝 '*700,title='A very long guild name '*500)
    assert all(len(gemma.load_tokenizer().encode(p.request).ids)<=480 for p in result.passages)
    assert 'title: A very long guild name' in result.passages[0].request
