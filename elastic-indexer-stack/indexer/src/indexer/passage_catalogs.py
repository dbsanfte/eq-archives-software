"""Source-evidenced catalog structures on major database sites.

Promote actual column labels and remove proven menus. Never select a guessed
largest content region or reconstruct missing data from URL parameters.
"""
import re
from bs4 import BeautifulSoup
from .passage_forums import words, row_cells

STATS={'name','item','id','title','category','date','downloads','author','class','race',
       'level','server','guild','rank','hp','mana','ac','atk','str','sta','agi','dex',
       'wis','int','cha','pr','mr','dr','fr','cr','average','resists','weight','size',
       'slot','dmg','damage','dly','delay','effect','classes','races','deity','notes',
       'description','skill','type','rating','cost','price','recommended level',
       'required level','instrument','song','location','zone','merchant','req. lvl',
       'rec. lvl','rarity','sv magic','sv fire','sv cold','sv disease','sv poison'}
STATS.add('bard only')


def eqdiva_panels(soup,excluded):
    """Bind repeated armor/resist sections to their captured panel headings."""
    changed=False
    for table in list(soup.find_all('table')):
        rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
        titles={}
        for index,row in enumerate(rows):
            cells=row_cells(row)
            if len(cells)!=1 or not cells[0].get('colspan') or not cells[0].get('bgcolor'):continue
            name=words(cells[0])
            if (name in {'HEAD','FACE','NECK','EAR','CHEST','ARMS','BACK','WAIST','LEGS','HANDS','SHOULDER','WRIST','FINGER','FEET'}
                    or re.search(r'Bard Quest Armor$',name,re.I)):
                if not cells[0].find(['a','img','table','pre','blockquote']):titles[index]=name
        if len(titles)<2 or any(r.parent is not table and r.parent.name!='tbody' for r in rows):continue
        if (table.find(['caption','thead','tfoot'])
                or any(isinstance(n,str) and n.strip() for n in table.children)
                or any(getattr(n,'name',None) not in ('tr','tbody',None) for n in table.children)):continue
        wrapper=soup.new_tag('div');current=None
        for index,row in enumerate(rows):
            if index in titles:
                heading=soup.new_tag('h2');heading.string=titles[index];wrapper.append(heading);current=None
                continue
            if current is None:current=soup.new_tag('table');wrapper.append(current)
            current.append(row.extract())
        table.replace_with(wrapper);changed=True
    if changed:excluded.append('eqdiva_armor_sections')
    return changed


def columns(soup,excluded):
    changed=False
    for table in soup.find_all('table'):
        rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
        for index,row in enumerate(rows[:4]):
            cells=row_cells(row);names=[words(c).lower().strip(' :') for c in cells]
            labels={x for x in names if x}
            if not labels:continue
            if (len(labels)>=2 and labels<=STATS and (labels&{'name','item','title','song','instrument'}
                    or len(labels&{'mr','fr','cr','pr','dr','ac'})>=5)
                    and any(words(r) for r in rows[index+1:]) and not row.find('table')):
                for cell in cells:cell.name='th'
                changed=True
            break  # A later data row must never become the table's header.
    if changed:excluded.append('catalog_column_headers')


def linked_menu(node,required):
    if node.find(['table','th','caption','h1','h2','h3','blockquote','pre','code','textarea']):return False
    links=node.find_all('a');labels={re.sub(r'\s*\[\+\]$','',words(a).lower().strip(' .!')) for a in links}
    if not required<=labels:return False
    text=words(node);linked=sum(len(words(a)) for a in links)
    return len(text)<3000 and linked>len(text)*.78 and not any(len(words(p))>80 for p in node.find_all('p'))


def project(soup,host,path,title,excluded):
    host=host.lower().removeprefix('www.').split(':')[0]
    if host=='graffe.com':
        identified=False
        for table in soup.select('table.spell'):
            header=table.select_one('td.spellhead3 a[title="Click here for more spell information"]')
            abstract=table.find(id=re.compile(r'^spellabstract\d+$'))
            if not header or not abstract:continue
            number=abstract['id'][13:]
            if soup.find(id='spelldata'+number) is None:continue
            heading=soup.new_tag('h2');heading.string='Spell: '+words(header)
            table.insert_before(heading);identified=True
        if identified:
            excluded.append('graffe_spell_boundaries')
            return title,'spell_catalog'
        return None
    if host=='everquest-online.com':
        identified=False;lists=[]
        for item in soup.find_all('li'):
            link=item.find('a',href=re.compile(r'/items/[^/]+-id\d+\.php$'),title=True)
            labels={words(b).lower().rstrip(':') for b in item.find_all(['b','strong'])}
            if not link or not {'stats','class','race'}<=labels:continue
            name=link['title'].strip()
            if not name or not words(item).startswith(name):continue
            lists.append(item.parent);item.name='section'
            heading=soup.new_tag('h2');heading.string='Item: '+name;item.insert(0,heading);identified=True
        for container in lists:
            if container.name=='ul' and not container.find('li',recursive=False):container.name='div'
        if identified:
            excluded.append('everquest_online_item_boundaries')
            return title,'item_catalog'
        return None
    if host not in {'eqinterface.com','eqrankings.com','maximumeq.com','eqdiva.com','eqspoilers.com','eqprices.com'}:return None
    # Forum messages are projected before these host-level catalog policies.
    changed=False
    if host=='eqinterface.com':
        for table in reversed(list(soup.find_all('table'))):
            if (table.parent and table.find(string=re.compile(r'^\s*Menu\s*$',re.I))
                    and linked_menu(table,{'home','forums','faq','downloads'})):
                table.decompose();excluded.append('eqinterface_category_menu');changed=True
    elif host=='eqrankings.com':
        for node in list(soup.find_all('div',id=re.compile(r'^menu\d+Content$',re.I))):
            if len(node.find_all('a'))>=2 and not node.find(['table','p','textarea','pre','blockquote']):
                links=node.find_all('a');residue=words(node)
                for a in links:residue=residue.replace(words(a),'')
                if not re.sub(r'[\W_]','',residue):
                    node.decompose();excluded.append('eqrankings_account_menu');changed=True
    elif host in {'maximumeq.com','eqdiva.com'}:
        if host=='eqdiva.com':
            changed=eqdiva_panels(soup,excluded)
            location=next((n for n in soup.find_all(['b','strong']) if words(n)=='Bard Song Locations'),None)
            if location and re.search(r'/locations\.asp$',path,re.I):
                name=location.find_next(['b','strong']);container=location.find_parent('td')
                if name and container and name in container.descendants and 'Continent:' in words(container):
                    title='Bard Song Locations: '+words(name);changed=True;excluded.append('eqdiva_location_context')
        required=({'the news','getting started','spells','trade skills','quests','guilds','faction'}
                  if host=='maximumeq.com' else {'home','forums','news','songs','instruments'})
        for node in reversed(list(soup.find_all(['table','td','div']))):
            if node.parent and linked_menu(node,required):
                node.decompose();excluded.append('catalog_navigation');changed=True
    elif host=='eqspoilers.com':
        # The database's own discussion label identifies its entity. It must
        # agree with a visible source heading; URL query values are not proof.
        discussion=soup.find(string=re.compile(r'^\s*Discuss\s+\S',re.I))
        entity=re.sub(r'^\s*Discuss\s+','',str(discussion),flags=re.I).strip() if discussion else ''
        heading=next((n for n in soup.find_all(['b','strong']) if words(n)==entity),None) if entity else None
        if heading and len(entity)<180:
            heading.name='h1';title=entity;changed=True;excluded.append('eqspoilers_entity_context')
        for cell in reversed(list(soup.find_all('td'))):
            if (cell.parent and cell.get('width')=='127' and cell.get('background')
                    and linked_menu(cell,{'home','bestiary','quests','spells','trade skills'})):
                cell.decompose();excluded.append('eqspoilers_database_menu');changed=True
    elif host=='eqprices.com' and re.search(r'/(?:scripts|cgi-bin)/showcomments\.asp$',path,re.I):
        for cell in soup.find_all('td'):
            if cell.find('table'):continue
            value=words(cell);match=re.fullmatch(r'(.{1,180}?)\s+Stats:\s+.+\s+Attributes:\s+.+',value,re.I)
            if match:
                title=match[1];changed=True;excluded.append('eqprices_item_context');break
    before=len(excluded);columns(soup,excluded)
    changed=changed or len(excluded)>before
    if not changed:return None
    return title,host.split('.')[0]+'_catalog'


def network_navigation(soup,excluded):
    """Shared legacy game-network mastheads, retaining described directories."""
    games={'eq','eq ii','eq2','daoc','mtg','ac','ac ii','ac2','ao','swg','sb','ffxi','ps','hz','wow','coh'}
    for node in reversed(list(soup.find_all('table'))):
        if (not node.parent or node.find_parent(['blockquote','pre','code','article'])
                or node.find(['th','caption','p','blockquote','pre','code','textarea'])):continue
        links=node.find_all('a');labels={words(a).lower() for a in links}
        if len(labels&games)<6 or not labels<=games:continue
        copy=BeautifulSoup(str(node),'html.parser')
        for link in copy.find_all('a'):link.decompose()
        residue=words(copy)
        if not re.sub(r'\bNETWORK\s+SITES\b|[\W_]','',residue,flags=re.I):
            node.decompose();excluded.append('game_network_navigation')
