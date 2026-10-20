"""Share archive link metadata without duplicating it in every paid campaign."""
from contextlib import closing
import heapq
import re
import sqlite3

from common import CrawlError

SHARED_GRAPH_VERSION = 'shared-graph-v1'


def shared_graph_root(store):
    if store.get('campaign_storage') != SHARED_GRAPH_VERSION:
        return None
    if store.root.parent.name != 'runs' or not re.fullmatch(r'[a-f0-9]{32}', store.root.name):
        raise CrawlError('Invalid shared discovery campaign location')
    return store.root.parent.parent


def link_rows(store):
    """Stream the shared graph and this run's additions, retaining exact sources."""
    query = 'SELECT url,source,evidence FROM links ORDER BY url,source'
    root = shared_graph_root(store)
    if root is None:
        yield from store.db.execute(query)
        return
    try:
        # mode=ro refuses to create an empty graph after a missing volume/move.
        with closing(sqlite3.connect((root / 'crawl.sqlite3').as_uri() + '?mode=ro',
                                     uri=True, timeout=1)) as shared:
            shared.row_factory = sqlite3.Row
            previous = None
            # Local evidence wins on an identical URL/source pair; independent
            # sources still contribute separately to a candidate's provenance.
            for row in heapq.merge(store.db.execute(query), shared.execute(query),
                                  key=lambda item: (item['url'], item['source'])):
                identity = (row['url'], row['source'])
                if identity != previous:
                    yield row
                previous = identity
    except sqlite3.Error:
        raise CrawlError('Shared discovery graph is unavailable; restore its storage before resuming') from None
