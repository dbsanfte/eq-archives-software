"""Source fidelity regressions for old, table-based archive pages."""

import pytest

from indexer.archive_handler import ArchiveHandler
from indexer.text_handler import TextHandler


def extract(tmp_path, html, relative_path="websites/example.org/20030812201431/archive.php?page=39"):
    path = tmp_path / "page.html"
    path.write_text(html, encoding="utf-8")
    handler = TextHandler(ArchiveHandler(), None, llm_enrichment_enabled=False)
    return handler._preprocess_website_file(str(path), relative_path)


def test_page_header_includes_alternate_wayback_link_for_local_index_file(tmp_path):
    relative_path = "websites/pub114.ezboard.com/20020601194540/flegacyofsteel43089general/index.html"
    text = extract(tmp_path, '<p>VEX THAL CLEARED!! ATEN HA RA DEAD!! AHR DAY!!</p>', relative_path)
    archive_url = "https://web.archive.org/web/20020601194540/http://pub114.ezboard.com/flegacyofsteel43089general"
    assert f"**Page URL:** {archive_url}/index.html" in text
    assert f"**Alternate Page URL:** {archive_url}" in text
    assert text.index("**Page URL:**") < text.index("**Alternate Page URL:**") < text.index("VEX THAL CLEARED!!")


@pytest.mark.parametrize("path", ["archive.php?page=39&stop=20", "news.html", "index.html?page=2"])
def test_page_header_omits_identical_alternate_and_preserves_query_string(tmp_path, path):
    text = extract(tmp_path, '<p>Complete article.</p>', f"websites/example.org/20030812201431/{path}")
    assert f"**Page URL:** https://web.archive.org/web/20030812201431/http://example.org/{path}" in text
    assert "Alternate Page URL:" not in text
    assert "Complete article." in text


def test_mixed_line_breaks_preserve_every_paragraph(tmp_path):
    # The reported FoH page mixes BR and self-closing br, which previously put
    # subsequent paragraphs inside br elements and discarded their contents.
    html = """<html><head><title>Guild news</title></head><body>
    <p><b><font>Check . . .<BR></b></font></p>
    <p>Opening paragraph about Serubane weaponry.<br /><br />
    Preparations for the next battle.<br /><br />
    The fight lasted 32:50 minutes.<br /><br />Final paragraph.</p>
    </body></html>"""
    text = extract(tmp_path, html)
    for paragraph in ("Opening paragraph", "Preparations", "32:50", "Final paragraph"):
        assert paragraph in text
    assert text.index("Opening paragraph") < text.index("Preparations") < text.index("32:50")


def test_layout_tables_keep_paragraphs_links_and_images_without_empty_grids(tmp_path):
    html = """<table width="100%" border="0"><tr><td><img src="banner.jpg"></td></tr></table>
    <table width="100%" border="0"><tr><td>
    <table><tr><td><a href="archive.php?page=38">Previous</a></td></tr></table>
    <table border="0"><tr><td background="edge.jpg"></td><td background="paper.jpg">
    <h2>Battle report</h2><p>First paragraph.</p><p>Second paragraph.</p>
    <a href="battle.jpg"><img src="battle.jpg" alt="Battle screenshot"></a>
    </td><td></td></tr></table></td></tr></table>"""
    text = extract(tmp_path, html)
    assert "| ---" not in text
    assert "First paragraph.\n\nSecond paragraph." in text
    assert "[Previous](archive.php?page=38)" in text
    assert "![Battle screenshot](battle.jpg)" in text
    assert "![](banner.jpg)" in text


@pytest.mark.parametrize("attributes", ['border="0"', 'role="presentation"', ''])
def test_data_tables_inside_layout_tables_keep_headers_cells_and_image_links(tmp_path, attributes):
    text = extract(tmp_path, f"""<table role="presentation"><tr><td><p>Loot statistics</p>
    <table {attributes}><caption>Weapons</caption><tr><th>Weapon</th><th>Damage</th></tr>
    <tr><td>Blade <img src="blade.jpg" alt="Blade"></td><td>42</td></tr></table>
    </td></tr></table>""")
    # An explicit presentation role wins; otherwise data semantics are kept.
    if 'role=' in attributes:
        assert "| ---" not in text
    else:
        assert "| Weapon | Damage |" in text
        assert "| Blade ![Blade](blade.jpg) | 42 |" in text
        assert "Weapons" in text


def test_plain_multicolumn_data_table_is_not_flattened(tmp_path):
    text = extract(tmp_path, '<table><tr><td>Sword</td><td>42</td></tr><tr><td>Bow</td><td>18</td></tr></table>')
    assert "| Sword | 42 |" in text
    assert "| Bow | 18 |" in text


def test_unmarked_layout_parent_does_not_consume_nested_data_headers(tmp_path):
    text = extract(tmp_path, '<table><tr><td><table><tr><th>Weapon</th><th>Damage</th></tr><tr><td><a href="blade.jpg"><img src="blade.jpg" alt="Blade"></a></td><td>42</td></tr></table></td></tr></table>')
    assert sum(line.startswith("| ---") for line in text.splitlines()) == 1
    assert "| Weapon | Damage |" in text
    assert "[![Blade](blade.jpg)](blade.jpg)" in text
