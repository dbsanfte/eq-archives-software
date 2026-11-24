"""Schedule two bounded preparation lanes and one archive writer."""

from common import now
from state import connect, unpack

SMALL_FILES = 2048
SMALL_BYTES = 128 * 1024 * 1024
SMALL_SLOTS = 4

# Snapshot metadata avoids repeatedly decoding every staged site's manifest.
# Previously approved rows use the same thresholds without a migration gate.
SMALL = f"""CASE WHEN s.manifest_sha256=b.manifest_sha256 THEN
    s.files<={SMALL_FILES} AND s.bytes<={SMALL_BYTES}
    WHEN json_array_length(b.manifest,'$.captures')<={SMALL_FILES} THEN
    (SELECT COALESCE(SUM(json_extract(value,'$.bytes')),0)
     FROM json_each(b.manifest,'$.captures'))<={SMALL_BYTES}
    ELSE 0 END"""


def size_receipt(store, identifier, expected, captures):
    store.db.execute('INSERT OR REPLACE INTO publication_sizes VALUES (?,?,?,?)',
                     (identifier, expected, len(captures), sum(c['bytes'] for c in captures)))


def claim(root, small):
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        row = store.db.execute(f"""SELECT o.* FROM operations o
            LEFT JOIN batches b ON b.id=json_extract(o.payload,'$.batch_id')
            LEFT JOIN publication_sizes s ON s.batch=b.id
            WHERE o.kind='publish' AND o.state='queued' AND (b.id IS NULL OR ({SMALL})=?)
            ORDER BY o.created,o.rowid LIMIT 1""", (small,)).fetchone()
        if row is None:
            return None
        operation = unpack(row)
        store.db.execute("UPDATE operations SET state='running',error=NULL,updated=? WHERE id=?",
                         (now(), operation['id']))
        store.db.commit()
        return operation


def current(store, operation):
    row = store.db.execute("""SELECT b.id FROM batches b JOIN operations o
        ON b.id=json_extract(o.payload,'$.batch_id')
        WHERE o.id=? AND o.kind='publish' AND o.state='running'
        AND b.state='publication_requested' AND b.manifest_sha256=json_extract(o.payload,'$.manifest_sha256')
        AND o.rowid=(SELECT MAX(n.rowid) FROM operations n
            WHERE n.kind='publish' AND json_extract(n.payload,'$.batch_id')=b.id)""", (operation['id'],)).fetchone()
    return row is not None
