"""Evidence-based forum layouts and utility pages for the search projection.

Recognize engines by several cooperating DOM features. Unknown layouts return
None and keep the generic content-preserving path. Archival readers are untouched.
"""
from dataclasses import dataclass
from html.parser import HTMLParser
import re
from bs4 import BeautifulSoup, Comment

DATE = re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}(?:\s*:\s*|\s+)\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?', re.I)
POST_DATE = re.compile(r'\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}(?:,?\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?)?',re.I)
LONG_DATE = re.compile(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}(?:,?\s+\d{1,2}:\d{2}\s*[ap]m)?',re.I)
SHORT_DATE = re.compile(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+(?:\d{4}|\d{2})(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?',re.I)


def dated(text):
    match=LONG_DATE.search(text) or SHORT_DATE.search(text) or POST_DATE.search(text)
    return match[0] if match else ''


@dataclass(frozen=True)
class Thread:
    html: str
    engine: str
    title: str = ''


def words(node):
    return ' '.join(node.get_text(' ',strip=True).split()) if node else ''


def row_cells(row):
    # html.parser retains a missing </td> as nested cells. Their nearest row
    # still identifies the two logical columns; nested authored tables do not.
    return [c for c in row.find_all(['td','th']) if c.find_parent('tr') is row]


def nonempty_link(node, pattern):
    return next((a for a in node.find_all('a',href=re.compile(pattern,re.I)) if words(a)),None)


def body_copy(node, *, signature_rule=False):
    body=BeautifulSoup(str(node),'html.parser')
    root=body.find()
    # Unclosed WWWThreads <p class=post> tags can contain the entire next
    # message in html.parser's tree. Bound the body at its next proven author
    # cell, before copying text or assigning it to the current speaker.
    marker=body.select_one('td[rowspan] a[name^=Post]')
    if marker and root and not marker.find_parent(['blockquote','pre','code']):
        boundary=marker
        while boundary.parent is not root and boundary.parent is not body:
            boundary=boundary.parent
        for sibling in list(boundary.next_siblings):sibling.extract()
        boundary.extract()
    for signature in list(body.select('.signature,.postsignature')):
        signature.decompose()
    if signature_rule:
        # phpBB's explicit signature separator, excluding quoted examples/code.
        for marker in list(body.find_all(string=re.compile(r'^\s*_{5,}\s*$'))):
            if marker.find_parent(['blockquote','pre','code']) or any('quote' in p.get('class',[]) for p in marker.parents):
                continue
            cursor=marker
            while cursor.parent is not None and cursor.parent is not body:
                for sibling in list(cursor.next_siblings):sibling.extract()
                cursor=cursor.parent
            marker.extract()
            break
    # phpBB renders quotations as presentation tables with a quote cell.
    for cell in list(body.select('td.quote')):
        table=cell.find_parent('table')
        if table is None:continue
        quote=body.new_tag('blockquote')
        label=next((b for b in table.find_all(['b','strong']) if cell not in b.parents and re.search(r'wrote:\s*$',words(b),re.I)),None)
        if label:
            heading=body.new_tag('p');heading.string=words(label);quote.append(heading)
        for child in list(cell.contents):quote.append(child.extract())
        table.replace_with(quote)
    return str(body)


def section(author,date,body,subject=''):
    soup=BeautifulSoup('','html.parser');tag=soup.new_tag('section');heading=soup.new_tag('h2')
    heading.string=' | '.join(v for v in ('Post by '+author if author else 'Post',date,subject) if v)
    tag.append(heading)
    fragment=BeautifulSoup(body,'html.parser')
    # A template cell/row is a container, not an authored data grid. Keep real
    # nested tables while preventing an orphan <td> from becoming a fake row.
    while any(getattr(n,'name',None) in ('td','th','tr') for n in fragment.contents):
        for node in list(fragment.contents):
            if getattr(node,'name',None) in ('td','th','tr'):node.unwrap()
    for text in list(fragment.find_all(string=True)):
        if isinstance(text,Comment) or text.find_parent(['pre','code']):continue
        escaped=re.sub(r'(?m)^( {0,3})(`{3,}|~{3,})',lambda m:m[1]+''.join('\\'+c for c in m[2]),str(text))
        if escaped!=str(text):text.replace_with(escaped)
    for node in list(fragment.contents):tag.append(node.extract())
    return str(tag)


class SourceRanges(HTMLParser):
    """Map DOM starts to original markup, including text outside damaged rows.

    HTMLParser supplies token boundaries without repairing table nesting or
    executing scripts. The DOM still supplies the cooperating format evidence.
    """
    def __init__(self, raw):
        super().__init__(convert_charrefs=False)
        self.raw=raw
        self.lines=[0]+[m.end() for m in re.finditer('\n',raw)]
        self.opens={};self.tables={};self.stack=[];self.tags=[];self.ends={}
        self.feed(raw);self.close()

    def position(self):
        line,column=self.getpos()
        return self.lines[line-1]+column

    def handle_starttag(self, tag, attrs):
        start=self.position()
        self.opens[start]=start+len(self.get_starttag_text())
        if tag=='table':self.stack.append(start)
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.tags.append((tag,start))

    def handle_endtag(self, tag):
        stop=self.raw.find('>',self.position())+1
        for i in range(len(self.tags)-1,-1,-1):
            if self.tags[i][0]==tag:
                self.ends[self.tags[i][1]]=stop
                del self.tags[i:]
                break
        if tag=='table' and self.stack:
            self.tables[self.stack.pop()]=stop

    def start(self, node):
        if node.sourceline is None:return None
        return self.lines[node.sourceline-1]+node.sourcepos

    def after(self, node):
        return self.opens.get(self.start(node))


def review_header(soup):
    return next((n for n in soup.find_all(['td','th'])
                 if not n.find_parent(['blockquote','pre','code','textarea'])
                 and re.sub(r'\s','',words(n)).lower()=='topicreview'),None)


def reply_quotes(soup):
    result=[]
    for field in soup.find_all('textarea'):
        if not words(field):continue
        fragment=BeautifulSoup('','html.parser');quote=fragment.new_tag('blockquote')
        quote.string=field.get_text();fragment.append(quote);result.append(str(fragment))
    return result


def after_rule(node):
    """Remove the proven leading metadata region, keeping all later content."""
    body=BeautifulSoup(str(node),'html.parser');rule=body.find('hr')
    if rule is None:return None
    current=rule
    while current.parent is not None:
        for previous in list(current.previous_siblings):previous.extract()
        current=current.parent
    rule.extract()
    return body_copy(body)


def snitz_review(soup, raw):
    header=review_header(soup)
    if header is None or not soup.select_one('textarea,input[type=password]'):return None
    if not (re.search(r'Snitz\s+Forums',words(soup),re.I)
            or soup.find('a',href=re.compile(r'forum\.snitz\.com|pop_profile\.asp',re.I))):return None
    table=header.find_parent('table')
    if table is None:return None
    # Some old captures close a row before its authored paragraphs. The next
    # proven author row, rather than the repaired DOM cell, bounds each body.
    source=SourceRanges(raw);start=source.start(header);rows=[]
    for row in soup.find_all('tr'):
        if source.start(row)<=start or row.find_parent(['blockquote','pre','code','textarea']):continue
        cells=row_cells(row)
        if len(cells)!=2 or not cells[0].has_attr('nowrap') or cells[0].get('valign','').lower()!='top':continue
        author=cells[0].find(['b','strong'])
        if not author and words(cells[0]):return None
        if source.after(cells[1]) is None:return None
        rows.append((row,cells,words(author)))
    if not rows:return None
    final_start=source.start(rows[-1][0])
    end=next((source.tables.get(source.start(t)) for t in [table,*table.find_parents('table')]
              if source.tables.get(source.start(t),0)>final_start),None)
    if end is None:return None
    posts=reply_quotes(soup)
    for i,(row,cells,author) in enumerate(rows):
        stop=source.start(rows[i+1][0]) if i+1<len(rows) else end
        body=BeautifulSoup(raw[source.after(cells[1]):stop],'html.parser')
        stamp=body.find(string=re.compile(r'^\s*Posted\s*[-:]\s*\d',re.I))
        date=DATE.search(' '.join(str(stamp).split())) if stamp else None
        # Only the leading platform metadata precedes this separator; genuine
        # tables, quotations and paragraphs after it remain in the body.
        if stamp and (separator:=stamp.find_next('hr')) is not None:
            if len(str(stamp))<100:
                parent=stamp.parent
                if parent.name in ('small','font','span') and words(parent)==' '.join(str(stamp).split()):parent.extract()
                else:stamp.extract()
            separator.extract()
        posts.append(section(author,date[0] if date else '',body_copy(body)))
    return Thread('\n'.join(posts),'snitz')


def phpbb(soup):
    posts=[];seen=set();recognized=False
    for node in soup.select('.postbody'):
        if any('postbody' in p.get('class',[]) for p in node.parents):continue
        row=next((r for r in node.find_parents('tr') if len(r.select('.name'))==1
                  and any(words(d).startswith('Posted:') for d in r.select('.postdetails'))),None)
        if row is None:
            if words(node):posts.append(section('','',body_copy(node)))
            continue
        if id(row) in seen:continue
        recognized=True
        seen.add(id(row))
        author=words(row.select_one('.name'))
        stamp=next(words(d) for d in row.select('.postdetails') if words(d).startswith('Posted:'))
        stamp=re.split(r'\s*Post subject:',stamp.removeprefix('Posted:').strip(),maxsplit=1)[0]
        bodies=[p for p in row.select('.postbody') if not any('postbody' in a.get('class',[]) for a in p.parents)]
        posts.append(section(author,stamp,''.join(body_copy(p,signature_rule=True) for p in bodies)))
    if posts and recognized:
        return Thread('\n'.join(posts),'phpbb',words(soup.select_one('.maintitle')))


def phpbb_print(soup):
    posts=[];recognized=False
    for body in soup.select('.postbody'):
        if any('postbody' in p.get('class',[]) for p in body.parents):continue
        row=body.find_parent('tr')
        header=row.find_previous_sibling('tr') if row else None
        if row is None or not row.select_one('td.printline'):return None
        metadata=words(header)
        author=re.search(r'\bAuthor:\s*(.*?)\s*,?\s*(?:Post)?Posted:',metadata,re.I)
        stamp=re.search(r'(?:Post)?Posted:\s*(.+)$',metadata,re.I)
        if author and stamp and dated(stamp[1]):recognized=True
        else:author=stamp=None
        posts.append(section(author[1].rstrip(', ') if author else '',stamp[1] if stamp else '',body_copy(body,signature_rule=True)))
    if posts and recognized:return Thread('\n'.join(posts),'phpbb',words(soup.select_one('.maintitle')))


def wwwthreads(soup):
    posts=[];seen=set()
    for body in soup.select('p.post'):
        profile=body.find_previous('td',attrs={'rowspan':'2'})
        if profile is None or not profile.select_one('a[name^=Post]'):
            # Partial recognition must never silently discard another body.
            return None
        if id(profile) in seen:continue
        author=nonempty_link(profile,r'showprofile\.(?:pl|php)')
        subject_cell=body.find_previous('td',class_='subjecttable')
        subject=subject_cell.find('b') if subject_cell else None
        if subject is None:return None
        name=words(author)
        if not name:
            guest=BeautifulSoup(str(profile),'html.parser')
            for metadata in guest.select('.small,small'):metadata.decompose()
            name=words(guest)
        seen.add(id(profile));date=DATE.search(words(profile))
        posts.append(section(name,date[0] if date else '',body_copy(body),words(subject)))
    if posts:return Thread('\n'.join(posts),'wwwthreads')


def snitz(soup,raw=None):
    if review_header(soup) is not None:
        return snitz_review(soup,raw) if raw is not None else None
    posts=[];recognized=False
    spans=soup.select('span.spnMessageText')
    # Snitz uses this class for profile links as well as messages. Counting
    # those links as bodies rejects the real post row, particularly when
    # missing closing cells attach later profile-stat rows to the same row.
    bodies=[]
    for node in spans:
        profile=nonempty_link(node,r'pop_profile\.asp')
        if node.get('id')!='msg' and profile and words(node)==words(profile):continue
        bodies.append(node)
    body_ids={id(node) for node in bodies}
    bodies=[node for node in bodies if not any(id(p) in body_ids for p in node.parents)]
    body_ids={id(node) for node in bodies}
    for body in bodies:
        row=None;author=None;date=None
        for candidate in body.find_parents('tr'):
            if sum(id(n) in body_ids for n in candidate.select('span.spnMessageText'))!=1:continue
            author=next((a for a in candidate.find_all('a',href=re.compile(r'pop_profile\.asp',re.I))
                         if words(a) and body not in a.parents),None)
            stamp=candidate.find(string=re.compile(r'Posted\s*[\-:]\s*\d',re.I))
            date=DATE.search(' '.join(str(stamp).split())) if stamp else None
            if author is not None and date:row=candidate;break
        if row is not None:
            recognized=True;posts.append(section(words(author),date[0],body_copy(body)))
        else:posts.append(section('','',body_copy(body)))
    if posts and recognized:
        for row in soup.find_all('tr'):
            cells=row_cells(row)
            if (len(cells)==2 and not cells[1].select_one('span.spnMessageText')
                    and words(cells[1]).startswith('Posted') and cells[1].find('hr')
                    and (nonempty_link(cells[0],r'pop_profile\.asp') or cells[0].find(['b','strong']))):
                return None  # Mixed/damaged templates retain the complete view.
        return Thread('\n'.join(reply_quotes(soup)+posts),'snitz')
    # A body-bearing template that could not be fully attributed must keep
    # the generic view, rather than partially match the older row adapter.
    if bodies:return None
    if not (re.search(r'Snitz\s+Forums',words(soup),re.I)
            or soup.find('a',href=re.compile(r'forum\.snitz\.com',re.I))):return None
    # Older Snitz topics have author/body cells and a metadata rule, without
    # the later spnMessageText wrapper. Match the row, not an arbitrary date.
    posts=[]
    for row in soup.find_all('tr'):
        cells=row_cells(row)
        if len(cells)!=2 or row.find_parent(['blockquote','pre','code']):continue
        author=nonempty_link(cells[0],r'pop_profile\.asp') or cells[0].find(['b','strong'])
        body=cells[1];stamp=body.find(string=re.compile(r'^\s*Posted\s*[-:]\s*\d',re.I))
        if stamp and stamp.find_parent(['blockquote','pre','code']):continue
        date=DATE.search(' '.join(str(stamp).split())) if stamp else None
        if not author or not words(body).startswith('Posted'):continue
        content=after_rule(body)
        if content is None:return None
        posts.append(section(words(author),date[0] if date else '',content))
    if posts:return Thread('\n'.join(reply_quotes(soup)+posts),'snitz')


def vbulletin_source(soup,raw):
    """Pair vB2's source comment regions despite broken table nesting."""
    if not raw or not re.search(r'<!--\s*Main\s+Post\s+Body\s*-->',raw,re.I):return None
    def regions(label):
        pattern=r'<!--\s*'+r'\s+'.join(map(re.escape,label.split()))+r'\s*-->'
        matches=list(re.finditer(pattern,raw,re.I))
        comments=[c for c in soup.find_all(string=lambda n:isinstance(n,Comment))
                  if ' '.join(str(c).split()).lower()==label.lower()
                  and not c.find_parent(['blockquote','pre','code','textarea'])]
        if len(matches)!=len(comments) or len(matches)%2:return []
        return [(a.start(),a.end(),b.start(),b.end()) for a,b in zip(matches[::2],matches[1::2])]
    bodies=regions('Main Post Body');users=regions('Postbit User Info');headers=regions('Postbit Header')
    if not bodies or len(bodies)!=len(users) or len(users)!=len(headers):return None
    source=SourceRanges(raw)
    stamps=[n for n in soup.find_all(string=re.compile(r'^\s*Posted\s+\d',re.I))
            if not n.find_parent(['blockquote','pre','code'])]
    posts=[]
    for i,(body,user,header) in enumerate(zip(bodies,users,headers)):
        if not header[3]<=user[0]<user[3]<=body[0] or i+1<len(headers) and body[3]>headers[i+1][0]:return None
        heading=BeautifulSoup(raw[header[1]:header[2]],'html.parser')
        if not heading.find('a',href=re.compile(r'showthread\.php\?.*postid=',re.I)) or not re.search(r'Post\s*#\d+\s+of\s+\d+',words(heading),re.I):return None
        profile=BeautifulSoup(raw[user[1]:user[2]],'html.parser');author=profile.find(['b','strong'])
        stamp=next((n for n in stamps if body[1]<=source.start(n.parent)<body[2]),None)
        if stamp is None:return None
        metadata=stamp.find_parent('td')
        if metadata is None or len(words(metadata))>120:return None
        date=dated(words(metadata))
        end=source.ends.get(source.start(metadata))
        if not date or end is None or not body[1]<end<=body[2]:return None
        content=BeautifulSoup(raw[end:body[2]],'html.parser')
        posts.append(section(words(author),date,body_copy(content),words(heading.find(['b','strong']))))
    return Thread('\n'.join(posts),'vbulletin')


def comment_region(start, label):
    """Only accept a complete sibling-delimited template region."""
    nodes=[]
    for node in start.next_siblings:
        if isinstance(node,Comment) and str(node).strip()=='/ '+label:
            return nodes
        if isinstance(node,Comment) and str(node).strip()==label:
            break
        nodes.append(node)
    return None


def vbulletin_header(body):
    # vB's numbered comments remain reliable when a damaged theme nests several
    # posts inside one table. Bind the anchor AND author menu to that same ID;
    # a missing current header cannot inherit a preceding post's identity.
    boundary=body.find_previous(string=lambda n:isinstance(n,Comment) and re.fullmatch(r'\s*post #\d+\s*',str(n)))
    if boundary:
        number=re.search(r'\d+',str(boundary))[0]
        anchor=body.find_previous('a',attrs={'name':'post'+number})
        author=body.find_previous(id='postmenu_'+number)
        if anchor and author:
            same=lambda n:n.find_previous(string=lambda x:isinstance(x,Comment) and re.fullmatch(r'\s*post #\d+\s*',str(x))) is boundary
            stamp=dated(words(anchor.find_parent('td')))
            if same(anchor) and same(author) and stamp:return words(author),stamp
    container=next((p for p in body.parents if re.fullmatch(r'post\d+',p.get('id',''))),None)
    if boundary and container is not None and container['id']!='post'+number:return None
    if container is not None:
        author=container.select_one('.bigusername,.username,[id="postmenu_'+container['id'][4:]+'"]')
        anchor=container.find('a',attrs={'name':re.compile(r'^post\d+$')})
        stamp=dated(words(anchor.find_parent('td'))) if anchor and anchor.find_parent('td') else ''
        if author and stamp:return words(author),stamp
        return None
    # Skinned vB 3 tables omit the table ID but retain a matching post anchor
    # and author-menu ID. Require both within the same, single-post container.
    for table in body.find_parents('table'):
        anchors=table.find_all('a',attrs={'name':re.compile(r'^post\d+$')})
        if len(anchors)!=1:continue
        anchor=anchors[0];author=table.find(id='postmenu_'+anchor['name'][4:])
        if boundary and anchor['name']!='post'+number:continue
        stamp=dated(words(anchor.find_parent('td')))
        if author and stamp:return words(author),stamp
    # Early vB 3 beta themes put every post in ONE presentation table. The
    # preceding header row and the body's own profile cell identify the post;
    # looking for an author in the whole table assigns every reply to user one.
    row=body.find_parent('tr')
    if row is None:return None
    cells=row.find_all('td',recursive=False)
    if len(cells)!=2 or cells[1] not in body.parents:return None
    header=next((p for p in row.previous_siblings if getattr(p,'name',None)=='tr'
                 and p.find('a',attrs={'name':re.compile(r'^post\d+$')})),None)
    if header is None:return None
    if boundary and not header.find('a',attrs={'name':'post'+number}):return None
    stamp=dated(words(header))
    author=cells[0].select_one('.bigusername,.username') or cells[0].find(['b','strong'])
    if stamp and words(author):return words(author),stamp
    return None


def vbulletin(soup):
    # vbHome's vBulletin archive skin uses explicit header/body row pairs,
    # without IDs or captured dates. It is shared software, not a host rule.
    legacy=soup.select('tr.archivebody')
    original=soup.select_one('.tbarchive a[href*="showthread.php"]')
    if legacy and original:
        posts=[];recognized=False
        for row in legacy:
            if any('archivebody' in p.get('class',[]) for p in row.parents):continue
            cells=row_cells(row)
            if len(cells)!=1:return None
            header=row.find_previous_sibling('tr');author=header.select_one('.archiveheader') if header else None
            if author:recognized=True
            posts.append(section(words(author),'',body_copy(cells[0])))
        if posts and recognized:
            caption=original.find_parent('tr')
            title=re.split(r'\s*-\s*Click\b',words(caption),maxsplit=1,flags=re.I)[0]
            return Thread('\n'.join(posts),'vbulletin',title)
    archive=soup.select('div.posttext')
    thread_link=soup.select_one('.largefont a[href*="showthread.php"]')
    if archive and thread_link:
        posts=[];recognized=False
        for body in archive:
            if any('posttext' in p.get('class',[]) for p in body.parents):continue
            container=body.find_parent('div',class_='post')
            author=container.select_one('.posttop > .username') if container else None
            date=container.select_one('.posttop > .date') if container else None
            if author is None:author=container.find('div',class_='username',recursive=False) if container else None
            numbered=(author and re.fullmatch(r'p\d+',body.get('id','')) and author.get('id')=='u'+body['id'][1:])
            if author and (numbered or date and dated(words(date))):
                recognized=True
            else:author=None
            posts.append(section(words(author),words(date) if author else '',body_copy(body)))
        if recognized:return Thread('\n'.join(posts),'vbulletin',words(thread_link))
    markers=[c for c in soup.find_all(string=lambda n:isinstance(n,Comment))
             if str(c).strip()=='message' and not c.find_parent(['blockquote','pre','code'])]
    posts=[];covered=set()
    for marker in markers:
        nodes=comment_region(marker,'message');header=vbulletin_header(marker)
        # A broken boundary/header must keep the complete generic view, rather
        # than drop one body or attach it to its neighbour's author.
        if nodes is None or header is None:return None
        for node in nodes:
            covered.add(id(node));covered.update(id(child) for child in getattr(node,'descendants',()))
        # Attachments are authored content outside the message region in some
        # templates. Signatures, edit/profile controls and menus are separate.
        for attachment in marker.parent.find_all(string=lambda n:isinstance(n,Comment)):
            if str(attachment).strip()=='attachments':
                attached=comment_region(attachment,'attachments')
                if attached is None:return None
                nodes+=attached
        body=BeautifulSoup(''.join(str(n) for n in nodes),'html.parser')
        for control in list(body.select('div.smallfont')):
            if (control.find('a',href=re.compile(r'(?:^|/)report\.php\?'))
                    and control.find('a',href=re.compile(r'(?:^|/)postings\.php\?'))):control.decompose()
        posts.append(section(*header,body_copy(body)))
    bodies=soup.select('[id^=post_message_]')
    if markers:
        if any(id(body) not in covered for body in bodies):return None
    else:
        for body in bodies:
            if any(str(p.get('id','')).startswith('post_message_') for p in body.parents):continue
            header=vbulletin_header(body)
            if header is None:return None
            posts.append(section(*header,body_copy(body)))
    if posts:return Thread('\n'.join(posts),'vbulletin')
    # vB 2 uses paired collapsed/expanded post divs instead of message comments.
    # Ignore the duplicate collapsed copy; preserve each complete body cell.
    for expanded in soup.find_all('div',id=re.compile(r'^p\d+e$')):
        number=expanded['id'][1:-1]
        anchor=expanded.find('a',attrs={'name':'post'+number})
        row=anchor.find_parent('tr') if anchor else None
        cells=row.find_all('td',recursive=False) if row else []
        collapsed=soup.find('div',id='p'+number+'h')
        author=cells[0].find(['b','strong']) if len(cells)==2 else None
        stamp=dated(words(collapsed))
        if len(cells)!=2 or not author or not stamp:return None
        posts.append(section(words(author),stamp,body_copy(cells[1])))
    if posts:return Thread('\n'.join(posts),'vbulletin')


def lithium(soup):
    bodies=soup.select('td.msg_text_cell');posts=[];recognized=False
    for body in bodies:
        if body.find_parent('td',class_='msg_text_cell'):continue
        table=next((t for t in body.find_parents('table') if len(t.select('td.msg_text_cell'))==1
                    and t.select_one('.msg_user_cell') and t.select_one('.msg_date_cell')),None)
        if table is None:return None
        user=table.select_one('.msg_user_cell')
        author=nonempty_link(user,r'view_profile\?user\.id=') or user.select_one('.auth_text')
        if author is None:
            author=user.find(['b','strong']) or user.find('span',style=re.compile(r'font-weight\s*:\s*bold',re.I))
        name=words(author)
        if not name:
            # Printer views omit profile links and often use an unstyled name.
            # Only the first source line is the author; rank/avatar/profile text
            # below its first break must not enter the attribution.
            first=[]
            for node in user.descendants:
                if getattr(node,'name',None)=='br':break
                if isinstance(node,str) and not isinstance(node,Comment):first.append(str(node))
            candidate=' '.join(' '.join(first).split())
            if len(candidate)<=120:name=candidate
        subject=next((c for c in table.select('td.subjectbar') if c.get('width')=='100%'),None)
        if subject is None:
            first=table.find('tr');cells=row_cells(first) if first else []
            if len(cells)==2 and not words(cells[0]):subject=cells[1]
        stamp=words(table.select_one('.msg_date_cell'))
        if name and dated(stamp):recognized=True
        posts.append(section(name,stamp,body_copy(body),words(subject)))
    if posts and recognized:return Thread('\n'.join(posts),'lithium')


def aspnet(soup):
    """ASP.NET Forums' PostList, profile and message-table roles across hosts."""
    tables=soup.find_all('table',id=re.compile(r'(?:^|_)PostList$',re.I));posts=[]
    for table in tables:
        headings=[words(c).lower() for c in table.select('th.PanelHeader')]
        if not headings or headings[0]!='author' or not any(h.startswith('thread:') for h in headings):continue
        for row in table.find_all('tr'):
            if row.find_parent('table') is not table:continue
            cells=row_cells(row)
            roles=[bool(set(c.get('class',[])) & {'PanelBody','PanelBodyAlternate'}) for c in cells]
            if len(cells)!=2:
                if any(roles) and words(row):return None
                continue
            if not all(roles):continue
            profile,content=cells;inner=content.find('table')
            rows=[r for r in inner.find_all('tr') if r.find_parent('table') is inner] if inner else []
            if len(rows)<2:return None
            subject=rows[0].select_one('.SmallBoldText')
            if subject is None or not subject.find('a',attrs={'name':re.compile(r'^\d+$')}):return None
            stamp=words(rows[0]).partition('Posted:')[2].strip()
            if not dated(stamp):return None
            author=nonempty_link(profile,r'UserProfile\.aspx\?UserName=')
            bodies=[]
            for i,body_row in enumerate(rows[1:]):
                # The explicit SmallText + leading rule is the signature slot;
                # genuine tables/quotes/attachments inside message rows remain.
                body_cells=row_cells(body_row)
                signature=body_cells[0].find('span',class_='SmallText',recursive=False) if len(body_cells)==1 else None
                if i and signature and signature.find('hr',recursive=False):break
                links=body_row.find_all('a')
                if links and all(re.search(r'/(?:AddPost|EditPost)\.aspx\?',a.get('href',''),re.I) for a in links) and not words(body_row):continue
                bodies.append(body_copy(body_row))
            posts.append(section(words(author),stamp,''.join(bodies),words(subject)))
    if posts:return Thread('\n'.join(posts),'aspnet')


def ultraboard(soup):
    if not soup.find('a',href=re.compile(r'UltraBoard\.cgi\?',re.I)):return None
    posts=[];seen=set()
    for row in soup.find_all('tr'):
        cells=row_cells(row)
        if len(cells)!=3 or row.find_parent(['blockquote','pre','code']):continue
        if not cells[0].find('img',alt=re.compile(r'^(?:New|Old) Message$',re.I)):continue
        subject=words(cells[1]);subject=re.sub(r'\s*\(modified \d+ times?\)\s*$','',subject,flags=re.I)
        if subject==words(cells[1]):continue
        inner=row.find_parent('table');table=inner.find_parent('table') if inner else None
        if table is None or id(table) in seen:return None
        rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
        if len(rows)!=4 or not rows[1].find('a',href=re.compile(r'UltraBoard\.cgi\?.*Action=ShowProfile',re.I)):return None
        stamp=dated(words(rows[3]))
        if not stamp:return None
        posts.append(section(words(cells[2]),stamp,body_copy(rows[2]),subject));seen.add(id(table))
    if posts:return Thread('\n'.join(posts),'ultraboard')


def ubbclassic(soup):
    if not (re.search(r'Ultimate\s+Bulletin\s+Board|UBB\.classic',words(soup),re.I)
            or soup.find('a',href=re.compile(r'(?:ultimatebb|Ultimate)\.cgi\?.*ubb=',re.I))):return None
    headers=[]
    for row in soup.find_all('tr'):
        if row.find_parent(['blockquote','pre','code']):continue
        cells=row_cells(row)
        if len(cells)==2 and words(cells[0]).lower()=='author' and words(cells[1]).lower().startswith('topic:'):
            headers.append(words(cells[1]).partition(':')[2].strip())
    if not headers or len(set(headers))!=1:return None
    posts=[]
    # UBB variants put the column header and each post in sibling tables, or
    # wrap all post tables inside one header table. The logical row and posted
    # metadata identify the same unit in both layouts.
    for row in soup.find_all('tr'):
        if row.find_parent(['blockquote','pre','code']):continue
        cells=row_cells(row)
        if len(cells)!=2:continue
        profile,body=cells;stamp_node=body.find(string=re.compile(r'^\s*posted\s+',re.I))
        separators=body.find_all('hr',recursive=False)
        if not separators:continue
        if stamp_node is not None and stamp_node.find_parent('tr') is not row:continue
        if stamp_node is None:
            if re.search(r'Member\s*#|Unregistered|Rate Member',words(profile),re.I) and words(body):return None
            continue
        stamp=dated(str(stamp_node));author=profile.find(['b','strong'])
        if not stamp or not author:return None
        nodes=[]
        for node in separators[0].next_siblings:
            if getattr(node,'name',None)=='hr' and re.match(r'\s*Posts:',words(node.find_next_sibling()),re.I):break
            nodes.append(str(node))
        posts.append(section(words(author),stamp,body_copy(BeautifulSoup(''.join(nodes),'html.parser'))))
    if posts:return Thread('\n'.join(posts),'ubbclassic',headers[0])


def ezboard(soup,host):
    if not host.endswith('ezboard.com'):return None
    posts=[];covered=set()
    for table in soup.find_all('table'):
        if any(id(parent) in covered for parent in table.parents):continue
        rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
        if not rows:continue
        cells=[row_cells(row) for row in rows]
        # Threaded mode shows one message above a directory of other replies.
        # Bind all three metadata rows and the explicit body, without treating
        # the reply-title directory as additional messages by this author.
        if (len(cells)>=4 and all(len(row)==2 for row in cells[:3])
                and [words(row[0]).lower() for row in cells[:3]]==['subject:','posted by:','posted at:']):
            stamp=DATE.search(words(cells[2][1]))
            bodies=[row[0] for row in cells[3:] if len(row)==1 and 'm' in row[0].get('class',[])]
            if stamp and len(bodies)==1:
                author=nonempty_link(cells[1][1],r'\.show(?:User)?PublicProfile(?:[?/#]|$)')
                author=author or cells[1][1].select_one('.title')
                posts.append(section(words(author),stamp[0],body_copy(bodies[0]),words(cells[0][1])))
                covered.add(id(table));continue
        header=next((i for i,row in enumerate(cells[:4])
                     if [words(c).lower() for c in row][:2]==['author','comment']),None)
        def metadata(row):
            if len(row)!=2:return False
            profile=nonempty_link(row[0],r'\.show(?:User)?PublicProfile(?:[?/#]|$)')
            reply=row[0].find('a',href=re.compile(r'\.showAddReplyScreen(?:FromWeb)?(?:[?/#]|$)',re.I))
            direct=lambda node:node is not None and node.find_parent('table') is table
            return bool(DATE.search(words(row[0])) and
                        (direct(profile) or direct(reply) and row[0].select_one('.title,.usertitle')))
        if header is None:
            # Localized skins retain the platform's DOM and action links.
            # An ordinary two-column data table is not sufficient evidence.
            if not any(metadata(row) for row in cells):continue
            header=next((i for i,row in enumerate(cells[:4]) if len(row)==2 and
                         all(c.name=='th' or 'headings' in c.get('class',[]) for c in row)),None)
            if header is None:
                if not metadata(cells[0]):continue
                header=-1  # Captured thread starts directly with a message.
        bodies=[]
        for row in cells[header+1:]:
            if len(row)!=2:
                if any(words(c) for c in row):return None
                continue
            if all(c.name=='th' or 'headings' in c.get('class',[]) for c in row):continue
            author=nonempty_link(row[0],r'\.show(?:User)?PublicProfile(?:[?/#]|$)')
            if author is None:author=next((a for a in row[0].find_all('a') if words(a)),None)
            name=words(row[0].select_one('.title')) or words(author)
            if not name:name=re.split(r'\b(?:Member|Posts:)\b',words(row[0]),maxsplit=1)[0].strip()
            date=DATE.search(words(row[0]))
            bodies.append(section(name,date[0] if date else '',body_copy(row[1])))
        if bodies:posts.extend(bodies);covered.add(id(table))
    if posts:return Thread('\n'.join(posts),'ezboard')


def reply_form(soup,path):
    if not re.search(r'(?:^|/)(?:post|posting|newreply|newpost)\.(?:asp|php|pl)$',path,re.I):return None
    fields=soup.find_all('textarea')
    if not fields or not soup.select_one('input[type=password],input[name=Username]'):return None
    # An unrecognized review is still authored content. Selecting only the
    # quote textarea would silently discard every other reviewed message.
    if review_header(soup) is not None:return None
    for row in soup.find_all('tr'):
        cells=row_cells(row)
        if (len(cells)==2 and cells[0].has_attr('nowrap') and cells[0].get('valign','').lower()=='top'
                and cells[0].find(['b','strong']) and words(cells[1]) and not cells[1].find('textarea')):
            return None
    if not re.search(r'Snitz Forums|vBulletin|phpBB|Ultimate Bulletin Board',words(soup),re.I):return None
    if soup.find(['blockquote','pre','code']):return None
    if soup.select_one('.postbody,.posttext,span.spnMessageText,.msg_text_cell,[id^=post_message_],p.post'):return None
    captured=[]
    for field in fields:
        if not words(field):continue
        fragment=BeautifulSoup('','html.parser');quote=fragment.new_tag('blockquote')
        quote.string=field.get_text();fragment.append(quote);captured.append(str(fragment))
    if captured:return Thread('\n'.join(captured),'forum_reply_field')


def extract(soup,host,path='',raw=None):
    # A forum widget can be embedded beside an independent article. The
    # engine view must not replace that page's unique non-message content.
    bodies='.postbody,span.spnMessageText[id=msg],p.post,.posttext,[id^=post_message_],.msg_text_cell'
    if any(words(node) and not node.select_one(bodies) for node in soup.select('article,main,[role=main]')):
        return None
    source_thread=vbulletin_source(soup,raw)
    if source_thread:return source_thread
    reviewed=snitz(soup,raw) if (soup.select_one('span.spnMessageText') or raw and re.search(r'Snitz|pop_profile\.asp',raw,re.I)) else None
    if reviewed:return reviewed
    for parser in (phpbb_print,phpbb,wwwthreads,vbulletin,lithium,ubbclassic,aspnet,ultraboard):
        result=parser(soup)
        if result:return result
    return ezboard(soup,host) or reply_form(soup,path)


def database_error_tables(soup):
    """Complete generated phpBB diagnostics outside authored message bodies."""
    tables=[]
    # phpBB can print an enormous SQL statement after a generated database
    # error. Recognize its complete engine layout before the small-page bound;
    # length itself must never turn a diagnostic dump into authored content.
    if (not soup.find(['article','main','blockquote','pre','code'])
            and soup.find('a',href=re.compile(r'(?:^|/)www\.phpbb\.com|https?://phpbb\.com',re.I))):
        for table in soup.select('table.forumline'):
            heading=table.select_one('th.thHead')
            if words(heading).lower()!='general error':continue
            diagnostic=table.select_one('span.gen')
            if diagnostic is None:continue
            if table.select_one('.postbody') or any('postbody' in p.get('class',[]) for p in table.parents):continue
            prefix=words(diagnostic)[:4096]
            debug=next((n for n in diagnostic.find_all(['b','u']) if words(n)=='DEBUG MODE'),None)
            authored=any(len(words(n))>100 and table not in n.parents for n in soup.find_all('p'))
            if (debug and not authored and re.search(r'\bSQL Error\s*:\s*\d+\b',prefix,re.I)
                    and re.search(r'\b(?:DELETE\s+FROM|SELECT\s+|INSERT\s+INTO|UPDATE\s+)\b',prefix,re.I)):
                tables.append(table)
    return tables


def excluded_page(soup,title,path):
    """Require page/engine evidence; a discussion of an error is still content."""
    if soup.find(['article','main']) or soup.select_one('.postbody,.post-content,.posttext,.archivebody,[id^=post_message_],span.spnMessageText,p.post,.msg_text_cell'):
        return None
    if any(words(t) for t in soup.find_all('textarea')):return None
    if database_error_tables(soup):return 'archived_access_or_error_page'
    text=words(soup);heading=words(soup.find(['h1','h2']))
    if len(text)>6000:return None
    known_title=re.fullmatch(r'(?:System Message:\s*Error|404(?: Not Found)?|403(?: Forbidden)?|Access Denied|Page Not Found|Site Temporarily Disabled)',title,re.I)
    disabled=heading.lower()=='site temporarily disabled' and 'this site has been disabled' in text.lower()
    body=words(soup.body or soup)
    deleted_ezboard=('ezboard' in text.lower()
                     and re.match(r'^(?:(?:www\.)?ezboard\.com\s+)?ERROR\s+The post you selected no longer exists\.',body,re.I)
                     and not soup.find(['pre','code','blockquote']))
    state=soup.find(string=re.compile(r'^\s*Unregistered Users Restricted from Viewing\s*$',re.I))
    restricted_ezboard=(state is not None and not state.find_parent(['blockquote','pre','code'])
                         and 'ezboard' in text.lower() and 'System Message' in text)
    message=soup.find(string=re.compile(r'^\s*vBulletin Message\s*$',re.I))
    missing_thread=(message is not None and not message.find_parent(['blockquote','pre','code'])
                     and re.search(r'No Thread specified\.\s*If you followed a valid link, please notify the (?:administrator|webmaster)\b',text,re.I))
    function=r'(?:[\w:]+\([^)]{0,1000}\)\s*:\s*)?'
    warning=r'Warning\s*:\s*'+function+r'failed to open stream:\s*No such file or directory\s+in\s+\S+\s+on line \d+\s*'
    required=r"(?:'[^']{1,1000}'|\"[^\"]{1,1000}\"|\S{1,1000})"
    fatal=(r'Fatal error\s*:\s*'+function+r'Failed opening required\s+'+required+
           r'(?:\s+\(include_path=[^)]{0,1000}\))?\s+in\s+\S+\s+on line \d+\s*')
    php_failure=not soup.find(['pre','code','blockquote']) and re.fullmatch(r'(?:'+warning+r')*'+fatal,body,re.I)
    engine_error=(re.search(r'phpBB\s*:\s*Critical Error\s+Could not (?:connect to the database|query config information)',text,re.I)
                  or 'vBulletin Message' in text and 'You are not logged in or you do not have permission to access this page' in text
                  or 'phpBB' in text and 'The topic or post you requested does not exist' in text
                  or heading.lower()=='error' and 'The post you selected no longer exists' in text
                  or 'requested access to a private forum' in text and 'ezboard' in text.lower())
    if known_title or disabled or engine_error or deleted_ezboard or restricted_ezboard or missing_thread or php_failure:return 'archived_access_or_error_page'
    if soup.find(['pre','code','blockquote']):return None
    # Reply forms can contain the only surviving quotation. Do not classify a
    # populated message field as an empty utility page.
    if any(words(t) for t in soup.find_all('textarea')):return None
    # Lithium's empty report popup has a distinct form contract. Keep saved
    # notes (protected above), quoted examples and authored reporting guides.
    if re.search(r'(?:^|/)notification$',path,re.I) and re.match(r'Notify Moderator\b',title,re.I):
        for form in soup.find_all('form'):
            if (form.find('textarea',attrs={'name':'notification.body'}) is not None
                    and all(form.find('input',attrs={'name':name}) is not None for name in ('board.id','message.id','notification.type'))
                    and not any(len(words(p))>40 and not p.find_parent('form') for p in soup.find_all('p'))):
                return 'forum_utility_page'
    utility_path=re.search(r'(?:^|/)(?:newpost|newuser|register|login|posting|post|newreply)\.(?:pl|php|asp)$',path,re.I)
    utility_title=re.match(r'(?:Make a New Post|New user|Register|Log ?in)(?:\b|\s*-)',title,re.I)
    if (utility_path or utility_title) and soup.find('form') and soup.select_one('textarea,input[type=password],input[name=Username],input[name=Subject]'):
        return 'forum_utility_page'
    return None
