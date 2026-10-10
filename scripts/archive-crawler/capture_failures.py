"""Capture-only failure policy; discovery and paid requests keep their limits."""
import re

from common import CrawlError

WORKER_INTERRUPTED = 'Capture worker stopped; saved progress will retry automatically'


def temporary(error):
    text = str(error)
    return bool(re.fullmatch(r'Wayback HTTP (?:408|422|425|429|5\d\d)(?: after bounded retries)?', text)
                or text.startswith(('Wayback connection failed', 'Shared Wayback transport stopped',
                                    'Downloader stopped', 'Worker stopped; resume explicitly', WORKER_INTERRUPTED,
                                    'CDX returned invalid JSON', 'Unexpected CDX schema',
                                    'Wayback repeated its catalog continuation', 'CDX repeated its continuation',
                                    'Wayback inventory returned a version outside 1999–2006',
                                    'Incomplete compressed response')))


def rate_limited(error):
    return bool(re.fullmatch(r'Wayback HTTP (?:422|429)(?: after bounded retries)?', str(error)))


def unavailable(error):
    return bool(re.fullmatch(r'Wayback HTTP (?:400|403|404|405|410|414|451)', str(error))
                or str(error).startswith(('Replay returned a different original URL',
                                         'Replay returned a different dated version',
                                         'Wayback returned a capture outside the requested tier',
                                         'Too many replay redirects', 'Empty source body')))


class FileRetries:
    """Durable per-URL attempts, with short runs during an upstream outage."""
    def __init__(self, store):
        self.db, self.streak = store.db, 0
        self.db.execute('''CREATE TABLE IF NOT EXISTS capture_file_attempts
                           (id TEXT PRIMARY KEY, attempts INTEGER NOT NULL)''')
        self.db.commit()

    def failed(self, key, error):
        if not isinstance(error, CrawlError):
            return None
        if unavailable(error):
            self.streak = 0
            return 'unavailable'
        # Rate limits, process failure and shutdown affect the shared service,
        # not a file's availability. Never consume a file retry for these.
        if (not temporary(error) or rate_limited(error)
                or str(error).startswith(('Shared Wayback', 'Downloader stopped', 'Worker stopped', WORKER_INTERRUPTED))):
            return None
        self.streak += 1
        self.db.execute('''INSERT INTO capture_file_attempts VALUES (?,1)
                           ON CONFLICT(id) DO UPDATE SET attempts=attempts+1''', (key,))
        attempts = self.db.execute('SELECT attempts FROM capture_file_attempts WHERE id=?', (key,)).fetchone()[0]
        return 'unavailable' if attempts >= 3 else 'retry'

    def succeeded(self):
        self.streak = 0
