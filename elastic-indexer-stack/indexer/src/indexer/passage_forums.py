"""Evidence-based forum layouts and utility pages for the search projection.

Recognize engines by several cooperating DOM features. Unknown layouts return
None and keep the generic content-preserving path. Archival readers are untouched.
"""
from dataclasses import dataclass
import re
from bs4 import BeautifulSoup, Comment

DATE = re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}(?:\s*:\s*|\s+)\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?', re.I)
POST_DATE = re.compile(r'\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}(?:,?\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?)?',re.I)
LONG_DATE = re.compile(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}(?:,?\s+\d{1,2}:\d{2}\s*[ap]m)?',re.I)


def dated(text):
    match=LONG_DATE.search(text) or POST_DATE.search(text)
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


def body_copy(node):
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
    for node in list(BeautifulSoup(body,'html.parser').contents):tag.append(node.extract())
    return str(tag)


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
        posts.append(section(author,stamp,''.join(body_copy(p) for p in bodies)))
    if posts and recognized:
        return Thread('\n'.join(posts),'phpbb',words(soup.select_one('.maintitle')))


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


def snitz(soup):
    posts=[];recognized=False
    for body in soup.select('span.spnMessageText[id=msg]'):
        if any(p.get('id')=='msg' and 'spnMessageText' in p.get('class',[]) for p in body.parents):continue
        row=None;author=None;date=None
        for candidate in body.find_parents('tr'):
            if len(candidate.select('span.spnMessageText[id=msg]'))!=1:continue
            author=next((a for a in candidate.find_all('a',href=re.compile(r'pop_profile\.asp',re.I))
                         if words(a) and body not in a.parents),None)
            stamp=candidate.find(string=re.compile(r'Posted\s*[\-:]\s*\d',re.I))
            date=DATE.search(' '.join(str(stamp).split())) if stamp else None
            if author is not None and date:row=candidate;break
        if row is not None:
            recognized=True;posts.append(section(words(author),date[0],body_copy(body)))
        else:posts.append(section('','',body_copy(body)))
    if posts and recognized:return Thread('\n'.join(posts),'snitz')


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
    archive=soup.select('div.posttext')
    thread_link=soup.select_one('.largefont a[href*="showthread.php"]')
    if archive and thread_link:
        posts=[];recognized=False
        for body in archive:
            if any('posttext' in p.get('class',[]) for p in body.parents):continue
            container=body.find_parent('div',class_='post')
            author=container.find('div',class_='username',recursive=False) if container else None
            if author and re.fullmatch(r'p\d+',body.get('id','')) and author.get('id')=='u'+body['id'][1:]:
                recognized=True
            else:author=None
            posts.append(section(words(author),'',body_copy(body)))
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
        subject=next((c for c in table.select('td.subjectbar') if c.get('width')=='100%'),None)
        stamp=words(table.select_one('.msg_date_cell'))
        if author and dated(stamp):recognized=True
        posts.append(section(words(author),stamp,body_copy(body),words(subject)))
    if posts and recognized:return Thread('\n'.join(posts),'lithium')


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
    posts=[]
    for table in soup.find_all('table'):
        rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
        if not rows:continue
        cells=rows[0].find_all(['td','th'],recursive=False)
        if [words(c).lower() for c in cells][:2]!=['author','comment']:continue
        for row in rows[1:]:
            cells=row.find_all('td',recursive=False)
            if len(cells)!=2:continue
            author=nonempty_link(cells[0],r'showUserPublicProfile')
            if author is None:author=next((a for a in cells[0].find_all('a') if words(a)),None)
            name=words(author) or words(cells[0].select_one('.title'))
            if not name:name=re.split(r'\b(?:Member|Posts:)\b',words(cells[0]),maxsplit=1)[0].strip()
            date=DATE.search(words(cells[0]))
            posts.append(section(name,date[0] if date else '',body_copy(cells[1])))
    if posts:return Thread('\n'.join(posts),'ezboard')


def reply_form(soup,path):
    if not re.search(r'(?:^|/)(?:post|posting|newreply|newpost)\.(?:asp|php|pl)$',path,re.I):return None
    fields=soup.find_all('textarea')
    if not fields or not soup.select_one('input[type=password],input[name=Username]'):return None
    if not re.search(r'Snitz Forums|vBulletin|phpBB|Ultimate Bulletin Board',words(soup),re.I):return None
    if soup.find(['blockquote','pre','code']):return None
    if soup.select_one('.postbody,.msg_text_cell,[id^=post_message_],p.post'):return None
    captured=[]
    for field in fields:
        if not words(field):continue
        fragment=BeautifulSoup('','html.parser');quote=fragment.new_tag('blockquote')
        quote.string=field.get_text();fragment.append(quote);captured.append(str(fragment))
    if captured:return Thread('\n'.join(captured),'forum_reply_field')


def extract(soup,host,path=''):
    # A forum widget can be embedded beside an independent article. The
    # engine view must not replace that page's unique non-message content.
    bodies='.postbody,span.spnMessageText[id=msg],p.post,.posttext,[id^=post_message_],.msg_text_cell'
    if any(words(node) and not node.select_one(bodies) for node in soup.select('article,main,[role=main]')):
        return None
    for parser in (phpbb,wwwthreads,snitz,vbulletin,lithium,ubbclassic):
        result=parser(soup)
        if result:return result
    return ezboard(soup,host) or reply_form(soup,path)


def excluded_page(soup,title,path):
    """Require page/engine evidence; a discussion of an error is still content."""
    if soup.find(['article','main']) or soup.select_one('.postbody,.post-content,[id^=post_message_],span.spnMessageText[id=msg],p.post,.msg_text_cell'):
        return None
    text=words(soup);heading=words(soup.find(['h1','h2']))
    if len(text)>6000:return None
    known_title=re.fullmatch(r'(?:System Message:\s*Error|404(?: Not Found)?|403(?: Forbidden)?|Access Denied|Page Not Found|Site Temporarily Disabled)',title,re.I)
    disabled=heading.lower()=='site temporarily disabled' and 'this site has been disabled' in text.lower()
    engine_error=(re.search(r'phpBB\s*:\s*Critical Error\s+Could not (?:connect to the database|query config information)',text,re.I)
                  or 'vBulletin Message' in text and 'You are not logged in or you do not have permission to access this page' in text
                  or 'phpBB' in text and 'The topic or post you requested does not exist' in text
                  or heading.lower()=='error' and 'The post you selected no longer exists' in text
                  or 'requested access to a private forum' in text and 'ezboard' in text.lower())
    if known_title or disabled or engine_error:return 'archived_access_or_error_page'
    if soup.find(['pre','code','blockquote']):return None
    # Reply forms can contain the only surviving quotation. Do not classify a
    # populated message field as an empty utility page.
    if any(words(t) for t in soup.find_all('textarea')):return None
    utility_path=re.search(r'(?:^|/)(?:newpost|newuser|register|login|posting|post|newreply)\.(?:pl|php|asp)$',path,re.I)
    utility_title=re.match(r'(?:Make a New Post|New user|Register|Log ?in)(?:\b|\s*-)',title,re.I)
    if (utility_path or utility_title) and soup.find('form') and soup.select_one('textarea,input[type=password],input[name=Username],input[name=Subject]'):
        return 'forum_utility_page'
    return None
