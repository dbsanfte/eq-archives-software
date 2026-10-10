"""Bounded, read-only pipeline status, independent of paginated portal lists."""
from capture_queue import QUEUE
from state import unpack

EVENTS = {
    'automatic_promoted': 'Automatically queued for capture',
    'automatic_saved': 'Saved for later · below automatic grade',
    'discovery_result': 'Candidate check saved',
    'automatic_settings': 'Automatic settings saved',
    'queue_capture_resume': 'Capture resume queued',
    'automatic_capture_retry': 'Automatic capture retry queued',
    'cancel_capture_resume': 'Capture resume cancelled',
}
KINDS = {'discover': 'Discovery', 'candidate_check': 'Evidence & grading',
         'capture': 'Capture', 'publish': 'Publication'}


def snapshot(store, worker=None):
    def site(identifier):
        row = store.db.execute('SELECT id,url FROM candidates WHERE id=?', (identifier,)).fetchone()
        return dict(row) if row else None

    def operation(row):
        if not row:
            return None
        value = unpack(row)
        payload, result = value['payload'] or {}, value['result'] or {}
        candidate = payload.get('id') or result.get('candidate_id')
        if payload.get('sites'):
            candidate = payload['sites'][0].get('id')
        return {**{key: value[key] for key in ('id', 'kind', 'state', 'error', 'updated')},
                'site': site(candidate),
                'payload': {key: payload[key] for key in ('automatic', 'target', 'fill_queue', 'max_candidates', 'min_grade', 'max_usd') if key in payload},
                'result': {key: result[key] for key in ('progress', 'phase', 'grade') if key in result}}

    discovery = operation(store.db.execute("""SELECT * FROM operations
        WHERE kind IN ('discover','candidate_check') ORDER BY updated DESC,rowid DESC LIMIT 1""").fetchone())
    next_capture = store.db.execute(f"""SELECT q.id,c.url,q.ready FROM ({QUEUE}) q
        JOIN candidates c ON c.id=q.id WHERE q.held=0 ORDER BY ready,ordinal LIMIT 1""").fetchone()
    retry = store.db.execute("""SELECT r.operation,r.attempts,r.retry_at,c.id,c.url FROM capture_retries r
        JOIN operations o ON o.id=r.operation JOIN candidates c ON c.id=json_extract(o.payload,'$.sites[0].id')
        WHERE o.state='interrupted' AND r.paused=0 AND r.retry_at IS NOT NULL ORDER BY r.retry_at LIMIT 1""").fetchone()
    # Publication has its own uncapped active operation. A publication_requested
    # batch may instead be paused, so it is not evidence of a busy worker.
    index = store.db.execute("""SELECT id,state,job,error,updated FROM batches
        WHERE state IN ('awaiting_review','published_waiting_index','indexing','index_budget_waiting')
        ORDER BY CASE state WHEN 'indexing' THEN 0 ELSE 1 END,created LIMIT 1""").fetchone()
    index = unpack(index) if index else None
    if index:
        row = store.db.execute("""SELECT id,url FROM candidates WHERE
            COALESCE(json_extract(coverage,'$.capture.review_id'),json_extract(coverage,'$.capture.batch_id'))=? LIMIT 1""", (index['id'],)).fetchone()
        index['site'] = dict(row) if row else None
    recent = []
    for row in store.db.execute("""SELECT * FROM operations WHERE state IN ('completed','interrupted')
            ORDER BY updated DESC,rowid DESC LIMIT 8"""):
        op = operation(row)
        recent.append({'id': op['id'], 'state': op['state'], 'at': op['updated'],
                       'label': KINDS.get(op['kind'], 'Operation') + (' completed' if op['state'] == 'completed' else ' paused'),
                       'site': op['site'], 'error': op['error']})
    for row in store.db.execute("""SELECT id,state,updated FROM batches WHERE state IN ('indexed','index_failed','index_preflight_failed')
            ORDER BY updated DESC,rowid DESC LIMIT 8"""):
        recent.append({'id': row['id'], 'state': row['state'], 'at': row['updated'],
                       'label': 'Indexing completed' if row['state'] == 'indexed' else 'Indexing needs attention'})
    # Read only the newest small event window, never source trees or raw details.
    for row in store.db.execute('SELECT id,candidate,action,created FROM events ORDER BY id DESC LIMIT 32'):
        if row['action'] in EVENTS:
            recent.append({'id': f"event-{row['id']}", 'state': row['action'], 'at': row['created'],
                           'label': EVENTS[row['action']], 'site': site(row['candidate'])})
    runtime = None
    if worker:
        runtime = {lane.value: thread.is_alive() and not worker.stop.is_set() for lane, thread in worker.threads.items()}
        runtime['wayback'] = worker.transport.healthy() and not worker.stop.is_set()
    return {'discovery': discovery, 'next_capture': dict(next_capture) if next_capture else None,
            'next_retry': dict(retry) if retry else None,
            'indexing': index, 'runtime': runtime,
            'recent': sorted(recent, key=lambda event: event['at'], reverse=True)[:8]}
