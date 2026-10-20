"""Initialize small per-run ledgers while keeping archive caches shared."""
from contextlib import closing
import os
from pathlib import Path
import tempfile

from common import CrawlError, Store, site_scope
from discovery_cache import SHARED_GRAPH_VERSION


def initialize(root, directory):
    directory = Path(directory)
    marker = directory / 'initialized'
    if marker.exists():
        database = directory / 'crawl.sqlite3'
        if not database.is_file() or not database.stat().st_size:
            raise CrawlError('Initialized campaign ledger is missing; restore it before resuming')
        return  # Legacy and current retries keep every original checkpoint.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # An interrupted initial snapshot cannot be mistaken for a paid run. Build
    # separately, checkpoint its WAL on close, then publish before the marker.
    with tempfile.TemporaryDirectory(prefix='.initialize-', dir=directory) as temporary:
        with closing(Store(temporary)) as run:
            run.db.execute('ATTACH DATABASE ? AS archive_cache',
                           ((Path(root).resolve() / 'crawl.sqlite3').as_uri() + '?mode=ro',))
            run.db.execute('BEGIN')
            for table in ('hosts', 'ezboard_aliases', 'ezboard_archive_boards',
                          'ezboard_archive_forums', 'sitepowerup_archive_boards'):
                run.db.execute(f'INSERT INTO {table} SELECT * FROM archive_cache.{table}')
            run.db.execute("""INSERT INTO meta SELECT * FROM archive_cache.meta
                WHERE key IN ('archive_sha','archive_repository')
                   OR key LIKE 'ezboard_archive_inventory:v1:%'
                   OR key LIKE 'sitepowerup_archive_inventory:v1:%'""")
            excluded = [site_scope(row[0]) for row in run.db.execute('SELECT url FROM archive_cache.candidates')]
            run.set('excluded_scopes', excluded)
            run.set('campaign_storage', SHARED_GRAPH_VERSION)
        # Only uninitialized files are replaceable. No sampling or paid calls
        # can happen until the marker exists; initialized runs never enter here.
        for suffix in ('-wal', '-shm'):
            (directory / ('crawl.sqlite3' + suffix)).unlink(missing_ok=True)
        os.replace(Path(temporary) / 'crawl.sqlite3', directory / 'crawl.sqlite3')
        # Persist the database name before publishing a marker that permits
        # sampling or paid work, including across an abrupt host shutdown.
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
            with marker.open('xb') as handle:
                marker.chmod(0o600)
                os.fsync(handle.fileno())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
