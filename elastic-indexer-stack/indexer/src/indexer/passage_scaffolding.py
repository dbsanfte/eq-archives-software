"""Conservative, host-independent detection of generated page scaffolding."""
import re
from bs4 import BeautifulSoup
from .passage_forums import words

PROTECTED='pre,code,blockquote,article,main,.postbody,.posttext,.msg_text_cell,[id^=post_message_]'


def expand_fallbacks(soup,excluded):
    # HTML parsers treat <noframes> as raw text. Reparse its real fallback HTML
    # so scripts/styles remain markup rather than escaped semantic source code.
    for node in list(soup.find_all('noframes')):
        value=node.get_text()
        if re.search(r'<(?:body|table|div|p|script)\b',value,re.I):
            node.replace_with(BeautifulSoup(value,'html.parser'))
            excluded.append('decoded_noframes_markup')


def clean(soup,excluded):
    for table in list(soup.find_all('table')):
        if not table.parent or table.find_parent(['blockquote','pre','code','article']) or table.select_one(PROTECTED):continue
        # A calendar has all seven weekday labels and many numeric day cells.
        # Named events or prose veto removal. This also handles ASP.NET widgets.
        cells=[c for c in table.find_all(['td','th']) if c.find_parent('table') is table]
        values=[words(c) for c in cells]
        days={v.lower() for v in values}&{'sun','mon','tue','wed','thu','fri','sat'}
        numbers=sum(bool(re.fullmatch(r'\d{1,2}',v)) for v in values)
        rest=' '.join(v for v in values if v.lower() not in days and not re.fullmatch(r'\d{1,2}',v))
        rest=re.sub(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b|\d{4}|[<>«»\s]','',rest,flags=re.I)
        if len(days)==7 and numbers>=14 and not rest:
            table.decompose();excluded.append('calendar_navigation')
    # Work from the smallest login wrapper, never drop an entire ASP.NET form
    # which may also own a complete news page or a populated quote/composer.
    for password in list(soup.select('input[type=password]')):
        for parent in password.parents:
            if parent.name not in ('div','td','table','form'):continue
            if len(words(parent))>180 or parent.select_one(PROTECTED) or parent.find('textarea'):break
            text=words(parent)
            residue=re.sub(r'please|log\s*in|username|user\s*name|password|remember|register|forgot(?:ten)?|your|\W','',text,flags=re.I)
            if not residue and parent.find('input',attrs={'name':re.compile('user|login',re.I)}):
                parent.decompose();excluded.append('login_controls');break
    # Image-only ad panels can include short linked alt labels. Require actual
    # external ad-delivery scripts and no authored paragraphs or data tables.
    for node in reversed(list(soup.find_all(['table','div']))):
        if not node.parent or node.find_parent(['pre','code','blockquote']) or node.select_one(PROTECTED):continue
        scripts=node.find_all('script',src=True)
        links=node.find_all('a',href=True)
        if not scripts or len(links)<3 or not node.find('img'):continue
        ad=any(re.match(r'^(?:https?:)?//',s['src'],re.I) and
               re.search(r'(?:[./_-])(?:ads?|adserver|serve|banner|placement|syndication|jwd_flt\d*)(?:[./_?=&-]|$)',s['src'],re.I)
               for s in scripts)
        text=words(node);linked=sum(len(words(a)) for a in links)
        if (ad and linked>=len(text)*.9 and len(text)<1000 and not node.find(['th','caption','pre','code','blockquote'])
                and not any(len(words(p))>40 for p in node.find_all(['p','li']))):
            node.decompose();excluded.append('hosting_advertising_panel')


def status(soup,title,excluded=()):
    if soup.select_one(PROTECTED) or any(words(t) for t in soup.find_all('textarea')):return None
    copy=BeautifulSoup(str(soup.body or soup),'html.parser')
    for n in list(copy.select('script,style,head,title')):n.decompose()
    text=words(copy)
    if not text and set(excluded)&{'calendar_navigation','login_controls'}:
        return 'generated_controls_only'
    if soup.find('frameset') and 'hosting_advertising_panel' in excluded:
        for node in list(copy.select('a,img,frameset,frame')):
            if node.parent:node.decompose()
        if not any(c.isalnum() for c in words(copy)):
            return 'advertising_frames_shell'
    if re.fullmatch(r'Error\. Page cannot be displayed\. Please contact your service provider for more details\.\s*\(\d+\)',text,re.I):
        return 'generated_host_error'
    sample_links={a['href'].lower().split('?')[0] for a in soup.find_all('a',href=True)}
    if (re.fullmatch(r'Welcome (?:To IIS 4\.0!|to Microsoft.? Windows NT.? 4\.0)',title,re.I)
            and 'Internet Information Server' in text and 'Samples' in text and len(text)<1600
            and {'/iissamples/default/learn.asp','/iissamples/default/samples.asp'}<=sample_links
            and soup.find('img',src=re.compile(r'/iissamples/default/IISTitle\.gif$',re.I))
            and 'iiswish@microsoft.com' in text):
        return 'default_webserver_page'
    terms=[a for a in soup.find_all('a',href=True) if re.search(r'(?:[?&])term=',a['href'])]
    popup=any(re.search(r'window\.open\s*\(\s*[\'"][^\'"]*(?:popup\.php|exitpage)',s.get_text(),re.I) for s in soup.find_all('script'))
    paid=[a for a in soup.find_all('a',href=True) if re.search(r'(?:clickthrough|/click(?:\.php|S\?))',a['href'],re.I)]
    search=soup.find(['input','select'],attrs={'name':'term'}) is not None
    if len(terms)>=10 and (popup or search and len(paid)>=6) and re.search(r'TOP\s*20\s*SEARCHES',text,re.I):
        # Boilerplate search advertisements, not an authored list of EQ links.
        for node in list(copy.select('a,form,select,input,button')):
            if node.parent:node.decompose()
        if len(' '.join(re.findall(r'\w+',words(copy))))<300:
            return 'archived_parking_page'
    return None
