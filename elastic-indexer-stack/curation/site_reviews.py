"""Independent site reviews referencing immutable files from capture batches."""
import json

from common import CrawlError, digest, original_url, within_scope
from state import connect, unpack, valid_id


def materialize(store, parent):
    """Caller owns the transaction; no sources are copied or downloaded."""
    manifest = parent['manifest']
    if not manifest or digest(manifest) != parent['manifest_sha256']:
        raise CrawlError('Capture manifest changed before site review')
    if any(capture.get('candidate_id') not in {site['id'] for site in manifest['sites']} for capture in manifest['captures']):
        raise CrawlError('Captured page has no approved site identity')
    multiple = len(manifest['sites']) > 1
    for site in manifest['sites']:
        captures = [capture for capture in manifest['captures'] if capture['candidate_id'] == site['id']]
        review_id = digest({'capture_batch':parent['id'],'site':site['id']})[:32] if multiple else parent['id']
        if multiple:
            reviewed = {**manifest, 'batch_id':review_id, 'review_unit':'site', 'capture_batch_id':parent['id'],
                        'capture_manifest_sha256':parent['manifest_sha256'], 'sites':[site], 'captures':captures,
                        'visited':[entry for entry in manifest.get('visited',[]) if entry[0] == site['id']],
                        'notes':[note for note in manifest.get('notes',[]) if within_scope(note['url'],site['scope'])]}
            existing = store.db.execute('SELECT manifest_sha256 FROM batches WHERE id=?',(review_id,)).fetchone()
            if existing and existing[0] != digest(reviewed):
                raise CrawlError('Site review identity collides with another manifest')
            store.db.execute("INSERT OR IGNORE INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,?,?)",
                             (review_id,json.dumps(reviewed),digest(reviewed),parent['created'],parent['updated']))
        row = store.db.execute('SELECT coverage FROM candidates WHERE id=?',(site['id'],)).fetchone()
        if row:
            coverage = json.loads(row['coverage'] or '{}')
            capture = coverage.get('capture',{})
            if capture.get('review_id') != review_id:
                coverage['capture'] = {**capture,'batch_id':parent['id'],'review_id':review_id,
                    'completed_at':capture.get('completed_at',parent['updated']), 'files':len(captures),
                    'pages':len({original_url(item['url']) for item in captures}), 'bytes':sum(item['bytes'] for item in captures)}
                store.db.execute('UPDATE candidates SET coverage=? WHERE id=?',(json.dumps(coverage),site['id']))
    if multiple:
        store.db.execute("UPDATE batches SET state='capture_group' WHERE id=?",(parent['id'],))


def migrate(root):
    with connect(root) as store:
        store.db.execute('BEGIN IMMEDIATE')
        for row in store.db.execute("SELECT * FROM batches WHERE state='awaiting_review'").fetchall():
            materialize(store,unpack(row))
        store.db.commit()


def get(store, review_id, candidate=None):
    row = store.db.execute('SELECT * FROM batches WHERE id=?',(valid_id(review_id),)).fetchone()
    if not row or not row['manifest'] or row['state'] in ('capturing','capture_group'):
        raise CrawlError('Captured site is not ready for review')
    result = unpack(row)
    manifest = result['manifest']
    if digest(manifest) != result['manifest_sha256']:
        raise CrawlError('Site review manifest changed')
    sites = manifest['sites']
    if candidate is not None:
        sites = [site for site in sites if site['id'] == candidate]
    if len(sites) != 1:
        raise CrawlError('Choose one captured site to review')
    slots = [slot for slot,item in enumerate(manifest['captures']) if item['candidate_id'] == sites[0]['id']]
    # Already approved legacy mixed batches retain their original publication/Job
    # identity. Their sources can still be read one site at a time.
    result['source_slots'] = slots
    result['manifest'] = {**manifest,'sites':sites,'captures':[manifest['captures'][slot] for slot in slots]}
    result['page_identities'] = [original_url(manifest['captures'][slot]['url']) for slot in slots]
    return result
