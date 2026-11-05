"""Readable Markdown from legacy HTML, retaining source text and image links."""

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter


TEXT_EXTRACTION_VERSION = "html-2026-10-05"
WEBSITE_EXTRACTION_VERSION = "html-2026-11-05-regional-decoding"


def extraction_version(record_id):
    # Only website headers changed. Completed newsgroups and mailing lists keep
    # their existing extraction checkpoints when the broad job resumes.
    return WEBSITE_EXTRACTION_VERSION if record_id.startswith("websites/") else TEXT_EXTRACTION_VERSION


def is_layout_table(table):
    """Flatten presentation/nested/one-column tables, retain data semantics."""
    if table.get("role", "").lower() in ("presentation", "none"):
        return True
    if any(element.find_parent("table") is table
           for element in table.find_all(["th", "caption", "thead", "tfoot"])):
        return False
    if table.find("table") or table.has_attr("background"):
        return True
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    cells = [row.find_all(["td", "th"], recursive=False) for row in rows]
    if any(cell.has_attr("background") for row in cells for cell in row):
        return True
    if all(not cell.get_text(strip=True) for row in cells for cell in row):
        return True
    # Empty border slices, navigation and article containers are not data grids.
    return all(sum(bool(cell.get_text(strip=True) or cell.find("img"))
                   for cell in row) <= 1 for row in cells)


def website_markdown(html):
    soup = BeautifulSoup(html, "html.parser")
    layouts = [table for table in soup.find_all("table") if is_layout_table(table)]
    for table in reversed(layouts):
        # Changing only this table's structural elements leaves nested data
        # tables intact. Block boundaries preserve distinct article paragraphs.
        for element in table.find_all(["td", "th", "tr", "tbody", "thead", "tfoot"]):
            if element.find_parent("table") is table:
                element.name = "div"
        table.name = "div"
    # Damaged old HTML can nest hundreds of formatting tags. Bound converter
    # stack depth without raising Python's process-wide recursion limit or
    # discarding text, image links, code or data cells. Only deep containers
    # lose redundant nesting; explicit line breaks retain block boundaries.
    stack=[(soup,0)]
    protected={'table','tr','td','th','thead','tbody','tfoot','a','img','pre','code','kbd','samp'}
    blocks={'div','p','blockquote','section','article','center','ul','ol','li'}
    while stack:
        node,depth=stack.pop()
        children=list(node.children)
        if depth>=48 and node.name not in protected and node.parent:
            if node.name in blocks:
                node.insert_before(soup.new_tag('br'));node.insert_after(soup.new_tag('br'))
            node.unwrap();depth-=1
        stack.extend((child,depth+1) for child in reversed(children) if isinstance(child,Tag))
    return SourceMarkdownConverter().convert_soup(soup)


class SourceMarkdownConverter(MarkdownConverter):
    def convert_img(self, element, text, parent_tags):
        # Table cells normally reduce inline images to alt text, losing the
        # only source links for screenshot-only posts. Preserve every image URL.
        return super().convert_img(element, text, parent_tags - {"_inline"})
