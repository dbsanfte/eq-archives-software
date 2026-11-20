"""Source-bound supporting-file references; never a crawl of another site."""
import codecs
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from common import decode, original_url

DOWNLOAD = re.compile(r'\.(?:png|jpe?g|gif|svg|webp|ico|bmp|css|js|pdf|zip|gz|tar|rar|7z|exe|docx?|xlsx?|pptx?|rtf|txt|xml|mp3|wav|ogg|mid|midi|mp4|avi|mov|swf|fla|woff2?|ttf)(?:[?;]|$)', re.I)
CSS_URL = re.compile(r'''url\(\s*['"]?([^'"\s)]+)|@import\s+['"]([^'"]+)''', re.I)
PARKING_DEPENDENCY = 'Third-party asset referenced only by an archived domain-parking template'


def binary_source(raw):
    """Conservative byte evidence when old servers label downloads as text.

    Unicode BOMs and legacy Japanese/terminal escapes remain text. Unknown
    layouts are retained unless a binary signature or dense control bytes prove
    this is a file, rather than source suitable for text extraction/enrichment.
    """
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE,
                       codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF8)):
        return False
    if raw.startswith((b'(DWF V', b'PK\x03\x04', b'PK\x05\x06', b'\x1f\x8b', b'%PDF-',
                       b'\x7fELF', b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a')):
        return True
    sample = raw[:8192]
    controls = sum(value < 32 and value not in (7, 8, 9, 10, 12, 13, 26, 27) for value in sample)
    return controls >= 8 and controls * 20 > len(sample)


class Resources(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.base, self.urls, self.style = url, set(), False

    def add(self, value):
        if value and (url := original_url(value, self.base)):
            self.urls.add(url)

    def css(self, text):
        for match in CSS_URL.finditer(text):
            self.add(match[1] or match[2])

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'base' and attrs.get('href'):
            self.base = original_url(attrs['href'], self.base) or self.base
        if tag == 'style':
            self.style = True
        if tag in ('img', 'script', 'embed', 'source', 'audio', 'video', 'input', 'iframe'):
            self.add(attrs.get('src'))
        if tag in ('img', 'source'):
            for item in (attrs.get('srcset') or '').split(','):
                if item.split():
                    self.add(item.split()[0])
        if tag == 'link' and set((attrs.get('rel') or '').lower().split()) & {'stylesheet', 'icon', 'shortcut', 'preload'}:
            self.add(attrs.get('href'))
        if tag == 'object':
            self.add(attrs.get('data'))
        if tag == 'a' and ('download' in attrs or DOWNLOAD.search(attrs.get('href') or '')):
            self.add(attrs.get('href'))
        self.add(attrs.get('background'))
        self.add(attrs.get('poster'))
        self.css(attrs.get('style') or '')

    def handle_endtag(self, tag):
        if tag == 'style':
            self.style = False

    def handle_data(self, text):
        if self.style:
            self.css(text)


def parking_template(text, url):
    """Recognize an entire generated search lander before suppressing its assets."""
    if not re.search(r'Try\s+these\s+related\s+searches', text, re.I):
        return False
    from bs4 import BeautifulSoup
    from indexer.passage_forums import words
    from indexer.passage_scaffolding import PROTECTED

    soup = BeautifulSoup(text, 'html.parser')
    title = words(soup.title) if soup.title else ''
    host = urlsplit(url).hostname
    if (not host or title.casefold().rstrip('.') != host.casefold().rstrip('.')
            or soup.select_one(PROTECTED) or any(words(node) for node in soup.find_all('textarea'))):
        return False
    keywords = [link for link in soup.find_all('a', href=True)
                if re.search(r'[?&]keywords=[^&#]+', link['href'], re.I)
                and re.search(r'[?&]addargs=', link['href'], re.I)]
    forms = [form for form in soup.find_all('form')
             if {'keywords', 'addargs'} <= {field['name'].casefold() for field in form.select('input[name]')}
             and words(form).casefold() in ('', 'search the web:')]
    if len(keywords) < 8 or not forms:
        return False
    for link in soup.find_all('a', href=True):
        label, target = words(link).casefold(), link['href'].casefold()
        if (label == host.casefold() and target in (f'http://{host}/', f'https://{host}/')
                or target == f'mailto:{label}' and re.fullmatch(r'[\w.+-]+@[\w.-]+', label)):
            link.decompose()
    for node in [*keywords, *forms, *soup.select('script,style,head,title')]:
        if node.parent:
            node.decompose()
    residue = words(soup.body or soup)
    return bool(re.fullmatch(r'Try these related searches[:.]?\s*'
                             r'(?:This site planned for development[.]?\s*)?'
                             rf'For information regarding {re.escape(host)}, please contact:\s*'
                             r'(?:[\w.+-]+@[\w.-]+)?', residue, re.I))


def asset_references(root, capture, *, classify=False):
    from captures import TEXT_LIMIT, verified_source
    mime = (capture.get('content_type') or capture.get('mimetype') or 'text/html').split(';')[0].strip().lower()
    if mime not in ('text/html', 'application/xhtml+xml', 'text/css'):
        return set(), False
    if capture['bytes'] > TEXT_LIMIT:
        from common import CrawlError
        raise CrawlError('Supporting-file reference extraction exceeds its safe text size; capture remains incomplete')
    text, _ = decode(verified_source(root, capture), capture.get('content_type') or '')
    parser = Resources(capture['url'])
    if mime == 'text/css':
        parser.css(text)
    else:
        parser.feed(text)
    return parser.urls, classify and mime != 'text/css' and parking_template(text, capture['url'])


def references(root, capture):
    # Manifest validation uses original references, including older saved assets.
    return asset_references(root, capture)[0]
