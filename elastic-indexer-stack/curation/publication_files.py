"""Keep verified captures that exceed GitHub's blob limit in private staging."""

from common import CrawlError

GITHUB_BLOB_LIMIT = 100 * 1024 * 1024


def split(captures):
    public, private = [], []
    for capture in captures:
        if capture['bytes'] > GITHUB_BLOB_LIMIT:
            private.append({key: capture[key] for key in ('archive_path', 'url', 'timestamp', 'bytes', 'sha256')})
        else:
            public.append(capture)
    return public, private


def indexed_captures(batch):
    captures = batch['manifest']['captures']
    publication = batch.get('publication', {})
    if 'retained_private' not in publication:
        return captures  # Earlier confirmed publications include their entire manifest.
    public, private = split(captures)
    if not public or publication['retained_private'] != private:
        raise CrawlError('Published file exclusions do not match the approved manifest')
    return public
