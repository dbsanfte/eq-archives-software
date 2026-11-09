"""Additional shared forum templates evidenced by large archived communities."""
import re
from .passage_forums import Thread, words, row_cells, section, body_copy, dated, after_rule
from bs4 import BeautifulSoup


def snowboards(soup):
    posts=[];title=''
    for author in soup.select('a.AuthorLink[href*="user.asp?usr="]'):
        profile=author.find_parent('td');row=profile.find_parent('tr') if profile else None
        cells=row_cells(row) if row else []
        if len(cells)!=2 or cells[0] is not profile or profile.get('rowspan')!='3':continue
        metadata=words(cells[1]);stamp=re.search(r'Date Posted:\s*(.*?)\s*Subject:\s*(.*?)(?:\s*-?\s*Date Edited:|$)',metadata,re.I)
        if not stamp:return None
        body_row=row.find_next_sibling('tr');controls=body_row.find_next_sibling('tr') if body_row else None
        bodies=row_cells(body_row) if body_row else []
        if (len(bodies)!=1 or not any(c in ('BoardRowA','BoardRowB') for c in bodies[0].get('class',[]))
                or controls is None or not controls.find('a',href=re.compile(r'(?:^|/)post\.asp\?brd=',re.I))):return None
        title=title or stamp[2]
        posts.append(section(words(author),stamp[1],body_copy(bodies[0]),stamp[2]))
    if posts:return Thread('\n'.join(posts),'snowboards',title)


def magelo_forum(soup):
    posts=[];title=''
    heading=soup.find(string=re.compile(r'^\s*Messages in topic:',re.I))
    if heading and len(words(heading.parent))<200:title=re.sub(r'^Messages in topic:\s*','',words(heading.parent),flags=re.I)
    for body in soup.select('td.FORUM-MAIN-CEL-BODY'):
        container=body.find_parent('td',class_='FORUM-MAIN-CEL')
        row=container.find_parent('tr') if container else None
        cells=row_cells(row) if row else []
        if len(cells)!=2 or cells[1] is not container:return None
        author=cells[0].select_one('.WINDOW-BODY-TITLE')
        stamp=container.find(string=re.compile(r'^\s*Posted:\s*\d',re.I))
        if not author or not stamp or not dated(str(stamp)):return None
        posts.append(section(words(author),dated(str(stamp)),body_copy(body)))
    if posts:return Thread('\n'.join(posts),'magelo_forum',title)


def phpbb_one(soup):
    # phpBB 1 has no postbody class. It retains an ISO timestamp, profile /
    # edit action routes and Joined/Posts metadata in the matching left cell.
    if not soup.find('a',href=re.compile(r'(?:^|/)bb_profile\.php\?mode=view',re.I)):return None
    posts=[]
    for row in soup.find_all('tr'):
        if row.find_parent(['blockquote','pre','code']):continue
        cells=row_cells(row)
        if len(cells)!=2:continue
        author=cells[0].find(['b','strong']);profile=words(cells[0])
        stamp=cells[1].find(string=re.compile(r'^\s*Posted:\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}',re.I))
        if not stamp:continue
        if (not author or not re.search(r'Joined:.*Posts:',profile,re.I)
                or stamp.find_parent('tr') is not row or cells[1].find('hr') is None):return None
        fragment=BeautifulSoup(after_rule(cells[1]),'html.parser')
        # Footer actions follow their own rule; authored rules, attachments,
        # source code and quotations are retained.
        for link in list(fragment.find_all('a',href=re.compile(r'(?:bb_profile|editpost)\.php\?',re.I))):
            link.decompose()
        posts.append(section(words(author),re.sub(r'^\s*Posted:\s*','',str(stamp),flags=re.I).strip(),body_copy(fragment)))
    if posts:return Thread('\n'.join(posts),'phpbb1')
