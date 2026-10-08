"""Source-bound supporting-file references; never a crawl of another site."""
from html.parser import HTMLParser
import re

from common import decode, original_url

DOWNLOAD = re.compile(r'\.(?:png|jpe?g|gif|svg|webp|ico|bmp|css|js|pdf|zip|gz|tar|rar|7z|exe|docx?|xlsx?|pptx?|rtf|txt|xml|mp3|wav|ogg|mid|midi|mp4|avi|mov|swf|fla|woff2?|ttf)(?:[?;]|$)', re.I)
CSS_URL = re.compile(r'''url\(\s*['"]?([^'"\s)]+)|@import\s+['"]([^'"]+)''', re.I)


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


def references(root, capture):
    from captures import TEXT_LIMIT, verified_source
    mime = (capture.get('content_type') or capture.get('mimetype') or 'text/html').split(';')[0].strip().lower()
    if mime not in ('text/html', 'application/xhtml+xml', 'text/css'):
        return set()
    if capture['bytes'] > TEXT_LIMIT:
        from common import CrawlError
        raise CrawlError('Supporting-file reference extraction exceeds its safe text size; capture remains incomplete')
    text, _ = decode(verified_source(root, capture), capture.get('content_type') or '')
    parser = Resources(capture['url'])
    if mime == 'text/css':
        parser.css(text)
    else:
        parser.feed(text)
    return parser.urls
