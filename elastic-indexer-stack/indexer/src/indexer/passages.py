"""Versioned search passages, retaining the complete archival reader separately.

Selection uses source structure rather than LLM labels. Unknown layouts keep
their content; only positively identified scaffolding is excluded. Every plan
binds its selected view and source with hashes, and every passage has exact
offsets into that view. No network, inference, filesystem walks or source writes.
"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment

from . import gemma
from .chunking import SourceChunk, chunk_source
from .html_extraction import website_markdown, repair_script_markup
from . import passage_forums

VERSION = "eq-passages-480-v6"
MAX_TOKENS = 480
ASSETS = {".js", ".css", ".map", ".ico", ".svg", ".woff", ".woff2"}
CONTROLS = re.compile(r"^(?:reply(?: to (?:item|topic))?(?: with quote)?|quote|edit|delete|"
                      r"login|log in|log out|register(?: your free account(?: \(required\))?)?|"
                      r"view public profile|send (?:a private message|email) to .+|"
                      r"find all posts by .+|add .+ to your buddy list|send a correction|print|back to top)$", re.I)
DECORATION = re.compile(r"^(?:spacer|blank|pixel|bullet|divider|banner|logo|reply|quote|old|new|"
                        r".*(?:is offline|is online))$", re.I)
NAV = re.compile(r"(?:^|[-_ ])(?:nav(?:igation)?|sidebar|site-menu|site-header|site-footer|"
                 r"advert(?:isement)?|adbanner|breadcrumb)(?:$|[-_ ])", re.I)
FOOTER = re.compile(r"ONElist is THE place|e-mails exchanged daily|ONElist has nearly|"
                    r"Looking to expand your world\?|Yahoo! Groups Links|"
                    r"Your use of Yahoo! Groups is subject", re.I)
COLUMN_NAMES=re.compile(r'^(?:faction|positive quests|negative quests|member name|rank|name|spell|mana|item|effect|recipe|ingredients|username|from|joined|posts|email|url|icq|aim|yim|msn|replies|author|views|last post|topics|type|value|description|level|class|race|guild|location|zone):?$',re.I)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def useful_summary(text):
    if isinstance(text, list):
        text = " ".join(str(value) for value in text)
    if not isinstance(text, str) or not text.strip():
        return None
    if re.fullmatch(r"[\[\s]*still awaiting llm enrichment[.\]\s]*", text, re.I):
        return None
    return text


def identity(record_id):
    parts = record_id.split("/", 3)
    if len(parts) == 4 and parts[0] == "websites":
        return parts[1].lower(), urlsplit("http://archive.invalid/" + parts[3]).path
    return "", record_id


def _plain(text):
    text = re.sub(r"!?\[([^\]\n]*)\]\([^\n]*?\)", r"\1", text)
    return re.sub(r"[*_`#|]+", " ", text).strip()


def _short(text, budget=48):
    return _short_cached(text[:8192],budget)


@lru_cache(maxsize=1024)
def _short_cached(text,budget):
    text = " ".join(_plain(text).split())
    tokenizer = gemma.load_tokenizer()
    encoded=tokenizer.encode(text,add_special_tokens=False)
    if len(encoded.ids)>budget:
        text=text[:encoded.offsets[budget-1][1]]
    while len(tokenizer.encode(text, add_special_tokens=False).ids) > budget:
        words = text.rsplit(" ", 1)
        text = words[0] if len(words) > 1 else text[:max(1, len(text)//2)]
    return text


def document_prefix(title=""):
    return f"title: {_short(title) or 'none'} | text: "


@dataclass(frozen=True)
class Passage:
    start: int
    end: int
    text: str
    title: str = ""
    context: str = ""
    kind: str = "prose"

    @property
    def request(self):
        return document_prefix(self.title) + self.context + self.text


@dataclass(frozen=True)
class Plan:
    source_sha256: str
    html_sha256: str | None
    view: str
    title: str
    method: str
    exclusions: tuple
    passages: tuple
    version: str = VERSION

    def provenance(self):
        return {"version": self.version, "source_sha256": self.source_sha256,
                "html_sha256": self.html_sha256, "view_sha256": digest(self.view),
                "method": self.method, "exclusions": list(self.exclusions)}


def _encoded_advertising(soup,excluded):
    for node in list(soup.find_all(string=re.compile(r'<(?:script|noscript|iframe)\b',re.I))):
        if node.find_parent(['pre','code','blockquote']):continue
        if any(p.get('class') and set(p['class']) & {'postbody','posttext','msg_text_cell'}
               or str(p.get('id','')).startswith('post_message_') for p in node.parents):continue
        fragment=BeautifulSoup(str(node),'html.parser')
        scripts=fragment.find_all(['script','iframe'],src=True)
        fallback=fragment.find('noscript')
        # An external script plus image-only noscript fallback is an ad-code
        # fragment, not ordinary prose explaining HTML. Never decode/evaluate JS.
        if not scripts or fallback is None or not fallback.find('img'):continue
        for tag in list(fragment.find_all(['script','iframe','noscript'])):tag.decompose()
        if fragment.get_text(strip=True):continue
        node.extract();excluded.append('encoded_advertising_markup')


def _allakhazam(soup,title,excluded):
    navigation=re.compile(r'^(?:Bestiary|Jump|Search(?: Comments)?|Statistics|Allakhazam.s|Buy [A-Za-z ]+|Search Results)\s*:',re.I)
    for table in reversed(list(soup.find_all('table'))):
        if not table.parent:continue
        labels=[c.get_text(' ',strip=True) for c in table.select('[id=headerrow]')]
        if (sum(bool(navigation.match(t)) for t in labels)>=3 and all(navigation.match(t) for t in labels)
                and len(table.find_all('form'))>=2 and not table.find(['h1','h2'])):
            table.decompose();excluded.append('legacy_aggregator_navigation')
    # Gossamer-style directory navigation and search forms are controls. Keep
    # actual category/site links and descriptions, including empty categories.
    menu={'home','add a link','modify a link','new links','cool links','top rated','random link','search'}
    for node in reversed(list(soup.find_all(['p','td','div']))):
        if not node.parent:continue
        links=node.find_all('a');labels={a.get_text(' ',strip=True).lower() for a in links}
        text=node.get_text(' ',strip=True)
        if len(labels & menu)>=5 and len(text)<500 and sum(len(a.get_text(strip=True)) for a in links)/max(1,len(text))>.65:
            node.decompose();excluded.append('directory_navigation')
    for form in list(soup.find_all('form')):
        if form.find('input',attrs={'name':re.compile(r'^(?:query|q)$',re.I)}) and len(form.get_text(strip=True))<200 and not form.find(['table','article','blockquote']):
            form.decompose();excluded.append('directory_search_control')
    for table in list(soup.find_all('table')):
        if not table.parent:continue
        rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
        cells=[c for r in rows for c in r.find_all(['td','th'],recursive=False)]
        posted=next((c for c in cells if c.get_text(' ',strip=True).startswith('Posted @')),None)
        author=next((c for c in cells if c.get_text(' ',strip=True).startswith('By:')),None)
        if author is None:continue
        if posted is None:
            posted=next((n for c in cells for n in c.find_all(['small','span']) if n.get_text(' ',strip=True).startswith('Posted @')),None)
        if posted is None:continue
        # Newer comments put author and body side by side on the same row.
        author_cells=author.find_parent('tr').find_all('td',recursive=False)
        if len(author_cells)==2 and author_cells[0] is author and author_cells[1] is not posted:
            bodies=[author_cells[1]]
        else:
            after=rows.index(posted.find_parent('tr'))+1
            bodies=[cell for row in rows[after:] for cell in row.find_all('td',recursive=False)
                    if cell is not author and cell.get_text(' ',strip=True) not in ('','Top','[Top]')
                    and not (cell.get('align')=='right' and re.fullmatch(r'\d+\s+Repl(?:y|ies)',cell.get_text(' ',strip=True),re.I))]
        subject=cells[0].get_text(' ',strip=True).split('Posted @',1)[0].strip()
        author_link=author.find('a',href=re.compile(r'(?:poster|user)'))
        if author_link:name=author_link.get_text(' ',strip=True)
        else:
            # Anonymous names precede the first profile line break. Do not
            # promote post counts and moderation scores into an author name.
            first_line=[]
            for node in author.descendants:
                if getattr(node,'name',None)=='br':break
                if isinstance(node,str) and not isinstance(node,Comment):first_line.append(str(node))
            name=' '.join(' '.join(first_line).split()).removeprefix('By:').strip()
        body=''.join(str(c) for c in bodies)
        if not any(c.get_text(' ',strip=True) or c.select_one('img[src],a[href],object,video,audio') for c in bodies):
            # An empty captured message is not its author's profile/score panel.
            # Its authored subject can still be useful; never invent a body.
            body='';excluded.append('empty_allakhazam_comment_body')
            if not subject:table.decompose();continue
        replacement=BeautifulSoup(passage_forums.section(name,posted.get_text(' ',strip=True),body,subject),'html.parser')
        table.replace_with(replacement);excluded.append('allakhazam_comment_controls')
    headers=[c for c in soup.select('[id=headerrow]') if not c.find_parent(class_='mform')]
    entity=next((c.get_text(' ',strip=True) for c in headers if c.get_text(strip=True)
                 and not navigation.match(c.get_text(' ',strip=True))
                 and not COLUMN_NAMES.fullmatch(c.get_text(' ',strip=True)) and not c.get_text(' ',strip=True).endswith(':')
                 and not re.search(r'By:|Posted @',c.get_text(' ',strip=True))),None)
    heading=next((h.get_text(' ',strip=True) for h in soup.find_all(['h1','h2'])
                  if not h.find_parent(class_='mform') and not navigation.match(h.get_text(' ',strip=True))
                  and not COLUMN_NAMES.fullmatch(h.get_text(' ',strip=True)) and not h.get_text(' ',strip=True).endswith(':')
                  and not re.match(r'^Post(?: by | \||$)',h.get_text(' ',strip=True))),None)
    title=entity or heading or title
    return title


def _html_view(raw, host, _preserve_layout=False, path=""):
    projection,repaired=repair_script_markup(raw)
    soup = BeautifulSoup(projection, "html.parser")
    excluded = []
    if repaired:excluded.append('repaired_script_boundaries')
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    source_title = title
    thread=passage_forums.extract(soup,host,path)
    if not thread:_encoded_advertising(soup,excluded)
    reason=None if thread else passage_forums.excluded_page(soup,title,path)
    if reason:
        return "",title,"archive_status",[reason]
    if thread:
        title=thread.title or title
        soup=BeautifulSoup(thread.html,"html.parser")
        excluded.append(thread.engine+"_posts")
        if thread.engine=='ezboard':excluded.append('ezboard_author_comment_structure')
    # Unknown posting templates can retain both a visible message preview and
    # a populated quoted-message field. Preserve both before removing controls.
    if not thread and re.search(r'(?:^|/)(?:post|posting|newreply|newpost)\.(?:asp|php|pl)$',path,re.I):
        for field in list(soup.find_all('textarea')):
            if field.get_text(strip=True):
                quote=soup.new_tag('blockquote');quote.string=field.get_text();field.replace_with(quote)
    for node in list(soup.find_all("title")):
        if not node.find_parent(["item","entry","rss","feed"]):
            node.decompose()
    # Known content markers are stronger evidence than a guessed largest div.
    marker = None if _preserve_layout else re.search(r"<!--\s*#?beginContent#?\s*-->(.*?)<!--\s*#?endContent#?\s*-->", projection, re.I|re.S)
    if marker:
        soup = BeautifulSoup(marker[1], "html.parser")
        excluded.append("outside_explicit_content_markers")
    selectors="script,style,noscript,head,select,input,button,textarea"
    if not _preserve_layout:selectors+=",nav,[role=navigation],[role=banner],[role=contentinfo]"
    for node in list(soup.select(selectors)):
        if node.parent:
            node.decompose()
    for node in list(soup.find_all(["div", "aside", "section", "header", "footer"])):
        if not node.parent:
            continue
        label = " ".join([str(node.get("id", "")), *node.get("class", [])])
        text=node.get_text(" ",strip=True)
        linked=sum(len(a.get_text(" ",strip=True)) for a in node.find_all("a"))
        labelled=bool(NAV.search(label)) or node.name in ("header","footer","aside")
        scaffolding=(linked/max(1,len(text))>.6 or not text and node.find(["img","iframe"]) is not None)
        attribution=node.select_one(".bigusername,.username,[rel=author]")
        if not _preserve_layout and labelled and scaffolding and attribution is None:
            excluded.append("explicit_navigation_or_decoration")
            node.decompose()
    # A real main/article landmark is useful on arbitrary modern sites. Avoid
    # choosing a tiny teaser, and retain the complete body when it is ambiguous.
    landmarks = soup.select("main,[role=main]")
    if not landmarks:
        landmarks = soup.select("article")
    landmark_ids={id(node) for node in landmarks}
    top = [node for node in landmarks if not any(id(parent) in landmark_ids for parent in node.parents)]
    outside=""
    if len(top)==1:
        outside=" ".join(str(node) for node in soup.find_all(string=True)
                         if not isinstance(node,Comment) and not any(parent is top[0] for parent in node.parents))
    if not _preserve_layout and len(top) == 1 and not _plain(outside) and len(top[0].get_text(" ", strip=True)) >= 100:
        soup = BeautifulSoup(str(top[0]), "html.parser")
        excluded.append("outside_main_landmark")
    primary=soup.find("h1")
    if primary and primary.get_text(" ",strip=True):
        title=primary.get_text(" ",strip=True)
    special = host.endswith(("allakhazam.com", "everlore.com", "castersrealm.com"))
    # These legacy aggregators use presentation tables for their sidebars.
    # Require both a link-heavy cell and several navigation labels; data tables
    # and curated directories must not disappear merely because they have links.
    if special and not _preserve_layout:
        for node in reversed(list(soup.find_all(["td", "table"]))):
            if not node.parent:
                continue
            links = node.find_all("a")
            text = node.get_text(" ", strip=True)
            labels = {a.get_text(" ", strip=True).lower().strip(". ") for a in links}
            signals = sum(any(term in label for label in labels) for term in
                          ("news", "search", "contact", "login", "links", "main"))
            linked = sum(len(a.get_text(" ", strip=True)) for a in links)
            database_menu=sum(any(term in label for label in labels) for term in ('by item','by zone','by faction'))==3
            data_grid=any(len(cells:=row.find_all(['td','th'],recursive=False))>1 and
                          any(re.fullmatch(r'[+\-]?\d[\d,.\s]*',c.get_text(' ',strip=True)) for c in cells)
                          for row in node.find_all('tr'))
            if ((len(links) >= 8 and signals >= 3 or len(links)>=3 and database_menu) and len(text) < 6000
                    and linked/max(1,len(text)) > .55 and not data_grid and not node.find('table')
                    and not node.select("#headerrow,th,caption")):
                excluded.append("legacy_aggregator_navigation")
                node.decompose()
    if host.endswith("allakhazam.com"):
        title = _allakhazam(soup,source_title,excluded)
    elif host.endswith("everlore.com"):
        heading = soup.find(["h1", "h2"])
        if heading:
            title = heading.get_text(" ", strip=True)
    # Modern forum engines and many Ezboard mirrors expose the message body.
    # Replace a single-post presentation table, retaining its source author/date.
    for node in ([] if thread else list(soup.select("[id^=post_message_],.postbody,.post-content,.postText"))):
        if not node.parent:
            continue
        parent=node.find_parent("table")
        container=parent if parent and len(parent.select("[id^=post_message_],.postbody,.post-content,.postText"))==1 else node
        author=container.select_one(".bigusername,.username,[rel=author]")
        metadata=container.get_text(" ",strip=True)
        date=re.search(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}(?:,?\s+\d{1,2}:\d{2}(?:\s*[AP]M)?)?",metadata,re.I)
        section=soup.new_tag("section");heading=soup.new_tag("h2")
        heading.string="Post"+(" by "+author.get_text(" ",strip=True) if author else "")+(" | "+date[0] if date else "")
        section.append(heading)
        if container is node:
            node.insert_before(heading)
        else:
            section.append(node.extract());container.replace_with(section)
            excluded.append("forum_profile_controls")
    # HTML feeds are not prose articles: retain each title/date/body together.
    feed = soup.find("rss") or soup.find("feed")
    if feed:
        sections = []
        for entry in feed.find_all(["item", "entry"]):
            name = entry.find("title")
            date = entry.find(["pubdate", "published", "updated"])
            body = entry.find(["description", "content", "summary"])
            sections.append("## " + (name.get_text(" ", strip=True) if name else "Feed entry") + "\n\n" +
                            (date.get_text(" ", strip=True)+"\n\n" if date else "") +
                            (website_markdown(body.get_text()) if body else entry.get_text(" ", strip=True)))
        if sections:
            return "\n\n".join(sections), title, "feed_entries", excluded
    for node in list(soup.find_all("a")):
        if node.parent is None:
            continue  # A malformed enclosing control link was already removed.
        if CONTROLS.fullmatch(node.get_text(" ", strip=True)):
            node.decompose()
            excluded.append("forum_or_account_control")
        else:
            # Visible link labels remain; full targets stay in text_full and
            # the captured archive, instead of consuming the embedding budget.
            node.attrs.pop("href", None)
    for node in list(soup.find_all("img")):
        alt = node.get("alt", "").strip()
        if not alt or DECORATION.fullmatch(alt):
            node.decompose()
        else:
            node.replace_with(alt)
    for comment in soup.find_all(string=lambda x: isinstance(x, Comment)):
        comment.extract()
    # Empty framing rows otherwise become a fabricated Markdown header above
    # the actual data headings. Promote only explicit/recognizable headings.
    for table in reversed(list(soup.find_all('table'))):
        rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
        kept=[]
        for row in rows:
            cells=row.find_all(['td','th'],recursive=False)
            if not any(c.get_text(strip=True) for c in cells):row.decompose()
            else:kept.append(row)
        if kept:
            cells=kept[0].find_all(['td','th'],recursive=False)
            if len(cells)>1 and all(COLUMN_NAMES.fullmatch(c.get_text(' ',strip=True)) or c.find(['b','strong']) for c in cells):
                for cell in cells:cell.name='th'
    view=website_markdown(str(soup))
    if not _preserve_layout and len(_plain(view))<50 and len(raw)>300:
        fallback, fallback_title, method, reasons=_html_view(raw,host,_preserve_layout=True,path=path)
        if len(_plain(fallback))>len(_plain(view))+100:
            return fallback,fallback_title,method,reasons+["conservative_layout_fallback"]
    return view, title, "forum_posts" if thread else "html_structure", excluded


def _text_view(text, record_id):
    excluded = []
    text = re.sub(r"^\*\*(?:Alternate )?Page URL:\*\*[^\n]*\n", "", text, flags=re.M)
    text = re.sub(r"<h1>(.*?)</h1>", r"# \1", text, flags=re.I|re.S)
    # This is a search view, not a replacement for the preserved reader source.
    text = re.sub(r"!?\[([^\]\n]*)\]\([^\n]*?\)", r"\1", text)
    if record_id.startswith(("newsgroups/", "mailing-lists/")):
        text=re.sub(r'<br\s*/?>','\n',text,flags=re.I)
        kept = []
        # A promotion can be appended to a long quoted conversation without
        # blank lines. Never discard that whole conversation to remove its ad.
        for line in text.splitlines(keepends=True):
            if FOOTER.search(line):
                excluded.append("mailing_service_promotion")
            else:
                kept.append(line)
        text = "".join(kept)
    return text, excluded


def _sections(view):
    """Contiguous runs with section, table and quotation boundaries."""
    lines = list(re.finditer(r"[^\n]*(?:\n|$)", view))
    lines = [m for m in lines if m.start() < m.end()]
    boundaries = {0, len(view)}
    headings = {}
    quoted_headings={}
    code_lines=set();fence=None
    for line in lines:
        marker=re.match(r'^ {0,3}(`{3,}|~{3,})',line[0])
        if fence:
            code_lines.add(line.start())
            if marker and marker[1][0]==fence[0] and len(marker[1])>=len(fence):
                boundaries.add(line.end());fence=None
        elif marker:
            fence=marker[1];code_lines.add(line.start());boundaries.add(line.start())
    reply_starts=set()
    quoted=False
    for line in lines:
        if line.start() in code_lines:continue
        value=line[0].strip()
        if re.match(r'(?:\\?>)+\s',value):quoted=True
        elif quoted and re.match(r'\d{1,3}[.)]\s+\S',value):
            boundaries.add(line.start());reply_starts.add(line.start());quoted=False
    previous = ""
    paragraph_kind=""
    for paragraph in re.finditer(r".+?(?:\n[ \t]*\n|\Z)",view,re.S):
        value=paragraph[0].lstrip()
        if not value:continue
        kind="code" if paragraph.start() in code_lines else "quote" if re.match(r"(?:\\?>)+\s",value) else "prose"
        if paragraph_kind and kind!=paragraph_kind and "quote" in (kind,paragraph_kind):
            boundaries.add(paragraph.start())
        # Replies often begin quoting immediately, without a blank line. Split
        # at the first quote marker, retaining wrapped continuation lines as
        # part of that quote rather than creating a fragment for every line.
        first_quote=next((m for m in re.finditer(r"(?m)^(?:\\?>)+[ \t]",paragraph[0])
                          if paragraph.start()+m.start() not in code_lines),None)
        if kind=="prose" and first_quote:
            boundaries.add(paragraph.start()+first_quote.start())
            kind="quote"
        paragraph_kind=kind
    for index, line in enumerate(lines):
        if line.start() in code_lines:
            previous='code'
            continue
        value = line[0].strip()
        heading = re.match(r"#{1,6}\s+(.+)", value)
        bold_heading=re.fullmatch(r"\*\*([^:*\n]{3,100}\s+[^:*\n]+)\*\*",value)
        if heading:
            boundaries.add(line.start());headings[line.start()] = _plain(heading[1])
        elif bold_heading:
            boundaries.add(line.start());headings[line.start()] = _plain(bold_heading[1])
        elif index+1 < len(lines) and re.fullmatch(r"[=-]{3,}", lines[index+1][0].strip()) and value:
            boundaries.add(line.start());headings[line.start()] = _plain(value)
        quoted_from=re.match(r"(?:\\?>[ \t]*)+From:\s*(.+)",value,re.I)
        if quoted_from:
            boundaries.add(line.start());quoted_headings[line.start()]="Quoted message from "+_short(quoted_from[1])
        kind = "table" if value.startswith("|") and value.count("|") >= 2 else "prose"
        if kind=='table' and index and not lines[index-1][0].strip():
            boundaries.add(line.start())
        if kind != previous and (kind != "prose" or value):
            boundaries.add(line.start())
        previous = kind if value else previous
    points = sorted(boundaries)
    section = ""
    post_scope="";quoted_scope=""
    for start,end in zip(points,points[1:]):
        if start in headings:
            heading=headings[start]
            if re.match(r'^Post(?: by |$| \|)',heading) or re.search(r'\bBy: .+ Posted @',heading):
                post_scope=heading;section=''
            else:section=heading
        value = view[start:end]
        first = value.lstrip().splitlines()[0] if value.strip() else ""
        attributed=bool(post_scope)
        kind = "code" if start in code_lines else "table" if first.startswith("|") and first.count("|")>=2 else "quote" if re.match(r"(?:\\?>)+\s",first) else "post" if attributed else "prose"
        quoted_scope=quoted_headings.get(start,quoted_scope) if kind=='quote' else ''
        context='\n\n'.join(part for part in (post_scope,section,quoted_scope) if part)
        yield start,end,context,kind


def _chunks(view, title, document_context="", strict_sections=False):
    result = []
    tokenizer = gemma.load_tokenizer()
    for start,end,heading,kind in _sections(view):
        body = view[start:end]
        context = document_context + (_short(heading)+"\n\n" if heading and _short(heading)!=_short(title) else "")
        if kind == "quote":
            context += "Quoted correspondence:\n"
        if kind == "table":
            lines = list(re.finditer(r"[^\n]*(?:\n|$)", body))
            lines = [m for m in lines if m.start()<m.end()]
            # The first meaningful row, including legacy <td> headings after
            # blank Markdown header rows. Never label dashes as column names.
            header=''
            for i,line in enumerate(lines[:8]):
                cells=[_plain(c).strip() for c in line[0].strip().strip('|').split('|')]
                if len(cells)>1 and all(c and COLUMN_NAMES.fullmatch(c) for c in cells):
                    header=line[0];break
                if i and re.fullmatch(r'[|:\-\s]+',line[0]) and '-' in line[0]:
                    candidate=lines[i-1][0]
                    if any(c.isalpha() for c in _plain(candidate)):
                        header=candidate;break
            table_context = _short(header,80)
            if table_context:
                context += "Table columns: " + table_context + "\n\n"
            prefix = document_prefix(title)+context
            group = []
            def flush():
                if group:
                    a,b=group[0].start(),group[-1].end()
                    result.append(Passage(start+a,start+b,body[a:b],title,context,kind))
                    group.clear()
            for line in lines:
                if len(tokenizer.encode(prefix+line[0]).ids)>MAX_TOKENS:
                    flush()
                    for chunk in chunk_source(line[0],tokenizer,prefix=prefix):
                        result.append(Passage(start+line.start()+chunk.start,start+line.start()+chunk.end,chunk.text,title,context,kind))
                else:
                    candidate = body[group[0].start():line.end()] if group else line[0]
                    if len(tokenizer.encode(prefix+candidate).ids)>MAX_TOKENS:
                        flush()
                    group.append(line)
            flush()
        else:
            prefix=document_prefix(title)+context
            for chunk in chunk_source(body,tokenizer,prefix=prefix):
                result.append(Passage(start+chunk.start,start+chunk.end,chunk.text,title,context,kind))
    packed=[]
    for passage in result:
        if packed and not strict_sections:
            previous=packed[-1]
            punctuation=(not any(c.isalnum() for c in passage.text) or not any(c.isalnum() for c in previous.text)) and not {previous.kind,passage.kind}&{'post','quote'}
            merge=previous.kind==passage.kind=='prose' or punctuation
            if not any(c.isalnum() for c in passage.text):context=previous.context
            elif not any(c.isalnum() for c in previous.text):context=passage.context
            else:context=previous.context if previous.context==passage.context else document_context
            if merge and previous.end==passage.start:
                body=view[previous.start:passage.end]
                if len(tokenizer.encode(document_prefix(title)+context+body).ids)<=MAX_TOKENS:
                    kind=passage.kind if not any(c.isalnum() for c in previous.text) else previous.kind
                    packed[-1]=Passage(previous.start,passage.end,body,title,context,kind)
                    continue
        packed.append(passage)
    return tuple(packed)


def prepare(text, record_id="", title="", *, html_source=None, mime_type=""):
    host,path=identity(record_id)
    excluded=[];method="source_text";raw_hash=None
    if host and (any(path.lower().endswith(suffix) for suffix in ASSETS) or path.lower().endswith("/robots.txt")):
        view="";excluded.append("supporting_asset")
    elif host and html_source and len(html_source)<=8*1024*1024 and ("html" in mime_type or "xml" in mime_type or re.match(r"\s*(?:<!DOCTYPE[^>]*>\s*)?<(?:html|body|rss|feed)\b",html_source,re.I)):
        view,extracted,method,excluded=_html_view(html_source,host,path=path)
        raw_hash=digest(html_source)
        title=extracted or title
    else:
        view,excluded=_text_view(text,record_id)
    document_context=""
    if record_id.startswith(("newsgroups/","mailing-lists/")):
        subject=re.search(r"(?:^|\n)#+\s*Subject:\s*(.+)",view)
        if subject:title=subject[1]
        header=re.match(r"(.*?)\n---\s*\n",view,re.S)
        if header:
            metadata=" ".join(line.lstrip("# ") for line in header[1].splitlines() if re.match(r"#+\s*(?:From|Date|Mailing-list|Newsgroups):",line))
            document_context=_short("Source message: "+metadata,64)+"\n\n" if metadata else ""
            view=view[header.end():]
    # Older layout-table conversions can put multiple equipment fields on a
    # single Markdown line. Recover those explicit labelled row boundaries.
    view=re.sub(r"\|\s*(\*\*(?:Primary|Secondary|Ranged|Neck|Ear|Face|Head|Shoulder|Arm|Finger|Wrist|Chest|Back|Feet|Legs|Waist|Hands):\*\*\s*\|)",r"\n| \1",view)
    view=view.strip()
    if view:view += "\n"
    elif text.strip():excluded.append("no_visible_content")
    title=_short(title)
    passages=_chunks(view,title,document_context,strict_sections=method=='feed_entries') if view.strip() else ()
    plan=Plan(digest(text),raw_hash,view,title,method,tuple(sorted(set(excluded))),passages)
    verify(text,plan)
    return plan


def verify(source,plan):
    if plan.version!=VERSION or digest(source)!=plan.source_sha256:
        raise ValueError("Passage plan differs from its source or policy")
    end=0;previous=-1;tokenizer=gemma.load_tokenizer()
    for passage in plan.passages:
        if (not 0<=passage.start<passage.end<=len(plan.view) or passage.start>end or passage.start<=previous
                or plan.view[passage.start:passage.end]!=passage.text
                or len(tokenizer.encode(passage.request).ids)>MAX_TOKENS):
            raise ValueError("Passage plan omits, changes or exceeds its selected source")
        previous=passage.start;end=passage.end
    if end!=len(plan.view) and plan.view.strip():
        raise ValueError("Passage plan omits selected content")
