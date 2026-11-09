"""Evidence-gated projections for two large, structured archive sites.

Only the semantic view changes. Unrecognized pages, forum messages, populated
panels and data values retain the ordinary conservative extraction path.
"""
import re
from bs4 import BeautifulSoup
from .passage_forums import words, row_cells
from . import passage_catalogs


def _heading(soup,node,title):
    heading=soup.new_tag('h2');heading.string=title;node.insert(0,heading)


def _header_table(table,labels):
    row=BeautifulSoup('<tr></tr>','html.parser').tr
    for label in labels:
        cell=BeautifulSoup('<th></th>','html.parser').th;cell.string=label;row.append(cell)
    table.insert(0,row)


def eqtraders(soup,path,title,excluded):
    # The article sidebar has the same button contract across the 2003–2006
    # site. Described link directories inside the article are content.
    branded=bool(re.search(r'EQTC|EQ\s*Traders|Traders.?\s*Corner',title,re.I))
    menu=soup.find('div',id='button')
    labels={words(a).lower() for a in menu.find_all('a')} if menu else set()
    legacy=bool(re.match(r'/(?:secrets|learn|location)/',path,re.I) and soup.find(['h1','h2','h3']))
    recognized=branded or legacy or bool(len(labels & {'home','about','faq','search','recipes','trade supplies','quests'})>=4)
    if not recognized:return None
    title=title or words(soup.find(['h1','h2','h3']))
    if menu and len(labels & {'home','about','faq','search','recipes','trade supplies','quests'})>=4:
        menu.decompose();excluded.append('eqtraders_navigation')
    if path.lower().endswith('/buttons.html'):
        links=soup.find_all('a');text=words(soup.body or soup)
        if len(links)>=8 and sum(len(words(a)) for a in links)/max(1,len(text))>.85:
            for a in links:a.decompose()
            excluded.append('eqtraders_navigation')
    for node in list(soup.find_all(['span','p'])):
        if not node.parent or node.find_parent(['blockquote','pre','code']):continue
        text=words(node)
        if text.startswith('All information, articles and graphics on this site are the copyright of EQ Traders Corner'):
            node.decompose();excluded.append('eqtraders_footer')
    for image in list(soup.find_all('img')):
        if image.get('alt','').lower() in ('eq traders corner','racknine'):
            image.decompose();excluded.append('eqtraders_navigation')
    # Real recipe-table headings, including old <td><b> variants. Retain each
    # complete row (ingredient counts, trivials, locations and comments).
    for table in soup.find_all('table'):
        rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
        if not rows:continue
        cells=row_cells(rows[0]);names=[words(c).lower().strip(':') for c in cells]
        columns={'item','recipe','ingredients','components','materials','clay','sketch',
                 'extra items','firing sheet(s)','trivial','yield','quantity','container','cost','notes'}
        if (set(names)&{'item','recipe'} and set(names)&{'ingredients','components','materials','clay','trivial'}
                and all(n in columns for n in names) and len(rows)>1):
            for cell in cells:cell.name='th'
            excluded.append('eqtraders_recipe_structure')
    # Older vendor pages use a one-word bold skill heading above each stock
    # list. Make those boundaries explicit so continuation chunks keep the
    # skill as well as the city in the document title.
    skills={'baking','brewing','fishing','fletching','jewelcraft','pottery','smithing',
            'spell research','alchemy','tailoring','tinkering','poison making'}
    if re.match(r'/(?:location|learn|secrets)/',path,re.I):
        for bold in list(soup.find_all(['b','strong'])):
            if words(bold).lower() not in skills or bold.find_parent(['table','blockquote','pre','code','li']):continue
            bold.name='h2'
    return title,'eqtraders_content'


def magelo(soup,path,title,excluded):
    old=soup.select_one('#character #block1')
    modern=soup.select_one('#profile #name')
    legacy=old is not None and bool(soup.select_one('#skill-data,#spell-data,#flags-data,#debug'))
    current=modern is not None and soup.select_one('#statTable') is not None
    if not (legacy or current):return None
    identity=words(old) if legacy else title.strip()
    if current and (not identity or identity.lower()=='magelo profile'):identity=words(modern)
    # All character information visible in the source remains visible. Do not
    # execute scripts to invent missing equipment/stats from an uncaptured UI.
    title='Magelo profile: '+identity
    for node in list(soup.select('.button,[id$=Button],[id$=ButtonImg],#skillOngletButtons,#settingsOngletContent,#altProfile,#mainProfile')):
        if node.parent and not node.find_parent(['pre','code','blockquote']):
            node.decompose();excluded.append('magelo_controls')
    if current:
        # These tabs have source-defined empty data areas. Remove their labels
        # only when no actual skills/effects/flags or biography were captured.
        for panel_id,data_id in (('skillOngletContent','skillContent'),('bioTabContent','bioContent'),('flagOngletContent','flagContent')):
            panel=soup.find(id=panel_id);data=soup.find(id=data_id)
            if panel is None or data is None or words(data) or data.find(['img','a','pre','code','blockquote']):continue
            copy=BeautifulSoup(str(panel),'html.parser')
            for node in list(copy.select('script,style,input,button,img')):node.decompose()
            labels=re.sub(r'\b(?:Combat|Trade|Lang|Worn|Clicky|Focus|Works indoor|Skills|Effects|Biography|Flags|Keys)\b|\W','',words(copy),flags=re.I)
            if not labels:panel.decompose();excluded.append('magelo_empty_panel')
    pairs=(('skill','Skills'),('spell','Spells'),('flags','Access flags'),('mod','Item modifiers'),('mod2','Secondary modifiers'))
    for key,label in pairs:
        header=soup.find(id=key+'-header') or soup.find(id=key+'-table-header')
        data=soup.find(id=key+'-data')
        if header is None or data is None:continue
        tables=header.find_all('table');target=data.find('table')
        rows=[r for t in tables for r in t.find_all('tr') if r.find_parent('table') is t]
        if len(rows)==1 and target is not None and row_cells(rows[0]):
            names=[words(c) for c in row_cells(rows[0])]
            _header_table(target,names);header.decompose()
            excluded.append('magelo_bound_table_headers')
        elif not words(data) and not data.find(['img','a','pre','code','blockquote']):
            header.decompose();excluded.append('magelo_empty_panel')
    headers={'skill','value','level','spell','item name','slot','from','access to:','granted:','abilities','cur/max','cost'}
    for table in list(soup.find_all('table')):
        if not table.parent:continue
        rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
        values=[[words(c) for c in row_cells(r)] for r in rows]
        # Header-only panes are controls. A single populated stat/data row is
        # still content, including zero and explicitly unknown values.
        if values and all(all(not x or x.lower() in headers for x in row) for row in values):
            table.decompose();excluded.append('magelo_empty_panel')
    sections={'skill':'Skills','spell':'Spells','flags':'Access flags','keys':'Keys','bio':'Biography',
              'focus':'Focus effects','adv':'Alternate advancement','leader':'Leadership',
              'debug':'Character calculations','magelo-info':'Character totals','bank':'Bank',
              'playerTabContent':'Character statistics','breakTabContent':'Stat breakdown',
              'bioContent':'Biography','aaOngletContent':'Alternate advancement',
              'skillContent':'Skills','flagContent':'Flags'}
    for key,label in sections.items():
        node=soup.find(id=key)
        if node is None:continue
        if words(node) or node.find(['img','a','pre','code','blockquote']):_heading(soup,node,label)
    # Empty stat cells in the old client are unpopulated labels, not character
    # values. Remove only the proven label/value rows in those exact panels.
    if legacy:
        for panel in soup.select('#block2,#str,#pr'):
            for row in list(panel.find_all('tr')):
                cells=row_cells(row)
                if len(cells)==2 and words(cells[0]) and not words(cells[1]):row.decompose()
    excluded.append('magelo_profile_structure')
    return title,'magelo_profile'


def project(soup,host,path,title,excluded):
    host=host.lower().removeprefix('www.').split(':')[0]
    if host=='eqtraders.com':return eqtraders(soup,path,title,excluded)
    if host in ('magelo.com','eq.magelo.com') and (path.endswith('/eq_view_profile.html') or re.match(r'/profile/\d+(?:/|$)',path)):
        return magelo(soup,path,title,excluded)
    return passage_catalogs.project(soup,host,path,title,excluded)
