"""Readable Markdown from legacy HTML, retaining source text and image links."""

import re

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter


TEXT_EXTRACTION_VERSION = "html-2026-10-05"
WEBSITE_EXTRACTION_VERSION = "html-2026-11-05-script-evidence"


def extraction_version(record_id):
    # Only website headers changed. Completed newsgroups and mailing lists keep
    # their existing extraction checkpoints when the broad job resumes.
    return WEBSITE_EXTRACTION_VERSION if record_id.startswith("websites/") else TEXT_EXTRACTION_VERSION


def repair_script_markup(raw):
    """Isolate broken inline scripts without executing JavaScript.

    Old generators sometimes write a literal closing script tag inside a JS
    string. Accept a later boundary only after that string closes and another
    closing tag is proven outside strings. Uncertain/truncated scripts retain
    the original HTML; ordinary markup cannot be consumed as JavaScript.
    This is only for the search projection, never the archival reader.
    """
    opening=re.compile(r'<script\b[^>]*>',re.I)
    closing=re.compile(r'</script\s*>',re.I)
    markup=re.compile(r'</?[a-z][a-z0-9:-]*(?:\s|/?>)',re.I)
    removed=[];cursor=0
    while match:=opening.search(raw,cursor):
        first=closing.search(raw,match.end())
        if first is None:break
        cursor=first.end()
        # A literal closing tag can occur halfway through generated iframe /
        # noscript HTML, not just immediately before the JS closing quote.
        # Scan only scripts which actually contain a nested script literal.
        if cursor==len(raw) or (raw[cursor] not in '\"\'`' and
                not opening.search(raw,match.end(),first.start())):continue
        i=match.end();limit=min(len(raw),i+65536)
        state='code';quote='';bracket=False;previous='';proved=False;embedded=False
        while i<limit:
            char=raw[i]
            if state=='string':
                inner=closing.match(raw,i)
                if inner and inner.start()==first.start():
                    embedded=True
                if char=='\\':i+=2;continue
                if char==quote:
                    if embedded and re.match(r'\s*(?:[+),;]|$)',raw[i+1:]):proved=True
                    state='code';previous='value'
            elif state=='regex':
                if char=='\\':i+=2;continue
                if char=='[':bracket=True
                elif char==']':bracket=False
                elif char=='/' and not bracket:state='code';previous='value'
                elif char in '\r\n':break
            else:
                end=closing.match(raw,i)
                if end:
                    if proved and end.start()>first.start():
                        removed.append((match.start(),end.end()));cursor=end.end()
                    break
                if state=='line':
                    if char in '\r\n':state='code'
                elif state=='comment':
                    if raw.startswith('*/',i):state='code';i+=2;continue
                elif raw.startswith('//',i) or raw.startswith('<!--',i):state='line'
                elif raw.startswith('/*',i):state='comment';i+=2;continue
                elif char in '\"\'`':state='string';quote=char
                elif char=='/' and (previous in ('','=',':','(',',','[','!','?',';','{')
                                    or re.search(r'\b(?:return|throw|case)\s*$',raw[max(match.end(),i-16):i])):
                    state='regex';bracket=False
                elif char=='<' and markup.match(raw,i):break
                elif not char.isspace():previous=char
            i+=1
    if not removed:return raw,0
    parts=[];cursor=0
    for start,end in removed:
        parts.extend((raw[cursor:start],'<script></script>'));cursor=end
    parts.append(raw[cursor:])
    return ''.join(parts),len(removed)


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
