"""Read-only portal stages over existing durable candidate/review transitions."""
from enum import Enum
from common import CrawlError, digest


class Stage(str, Enum):
    CANDIDATES = 'candidates'
    QUEUED = 'queued'
    CAPTURING = 'capturing'
    INDEXING = 'indexing'
    SAVED = 'saved'
    HISTORY = 'history'


REVIEW_STAGES = {
    'awaiting_review': Stage.INDEXING,
    'index_preflight_failed': Stage.INDEXING,
    'publication_requested': Stage.INDEXING,
    'published_waiting_index': Stage.INDEXING,
    'indexing': Stage.INDEXING,
    'index_failed': Stage.INDEXING,
    'index_budget_waiting': Stage.INDEXING,
    'indexed': Stage.HISTORY,
    'indexing_declined': Stage.HISTORY,
}
CANDIDATE_STAGES = {
    'approved_waiting_batch': Stage.QUEUED,
    'capture_resume_queued': Stage.QUEUED,
    'capturing': Stage.CAPTURING,
    'captured_awaiting_review': Stage.INDEXING,
    'approved_waiting_publication': Stage.INDEXING,
    'publication_requested': Stage.INDEXING,
    'published': Stage.INDEXING,
    'indexed': Stage.HISTORY,
    'indexing_declined': Stage.HISTORY,
    'deferred': Stage.SAVED,
    'rejected': Stage.HISTORY,
    'already_archived': Stage.HISTORY,
    'duplicate_candidate': Stage.HISTORY,
}


def decorate(store, rows):
    # Read status metadata without copying manifests or inspecting archive files.
    from capture_queue import held
    failures = held(store)
    states = dict(store.db.execute('SELECT id,state FROM batches'))
    captures = {row['candidate']: row for row in store.db.execute("""SELECT json_extract(site.value,'$.id') candidate,
        operations.id,operations.state,operations.error FROM operations,json_each(operations.payload,'$.sites') site
        WHERE kind='capture' ORDER BY operations.created,operations.rowid""")}
    for row in rows:
        if row['id'] in failures:
            row['capture_queue_error'] = failures[row['id']]
        capture = (row.get('coverage') or {}).get('capture') or {}
        review_id = capture.get('review_id') or capture.get('batch_id')
        if capture.get('continuation') and row['state'] in ('approved_waiting_batch', 'capture_resume_queued', 'capturing'):
            row['review_state'] = 'capture_continued'
        elif review_id in states:
            row['review_state'] = states[review_id]
        row['stage'] = REVIEW_STAGES.get(row.get('review_state'),
                         CANDIDATE_STAGES.get(row['state'], Stage.CANDIDATES)).value
        if row['stage'] in (Stage.CAPTURING, Stage.QUEUED) and row['id'] in captures:
            row['capture_operation_id'] = captures[row['id']]['id']
            row['capture_state'] = captures[row['id']]['state']
            row['capture_error'] = captures[row['id']]['error']
    return rows


def counts(rows):
    return {stage.value: sum(row['stage'] == stage.value for row in rows) for stage in Stage}


def search_matches(row, query):
    values = [row['url'], row['scope'], (row.get('rating') or {}).get('category', '')]
    values.extend(capture.get('title', '') for capture in row.get('captures') or [])
    return query.casefold() in ' '.join(values).casefold()


def grade_value(row):
    return (row.get('rating') or {}).get('grade', -1) if row.get('captures') else -1


def candidate_filter(rows, minimum=None, needs_grade=False):
    if minimum is not None and minimum not in ('0', '1', '2', '3'):
        raise CrawlError('Minimum grade must be between 0 and 3')
    rows = sorted(rows, key=lambda row: (-grade_value(row), min((c.get('tier') or 3 for c in row.get('captures') or []), default=3), -row['priority'], row['url']))
    if needs_grade:
        return [row for row in rows if grade_value(row) < 0 or not row.get('captures')]
    if minimum is not None:
        return [row for row in rows if grade_value(row) >= int(minimum) and row.get('captures')]
    return rows  # Preserve the unfiltered operator API.


def dismissal_preview(rows):
    candidates = sorted((row for row in rows if row['stage'] == Stage.CANDIDATES), key=lambda row: row['id'])
    return {'count': len(candidates), 'token': digest([
        [row['id'], row['manifest_sha256'], row['state'], row['decision']] for row in candidates])}
