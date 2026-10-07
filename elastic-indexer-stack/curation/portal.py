"""Read-only portal stages over existing durable candidate/review transitions."""
from enum import Enum


class Stage(str, Enum):
    CANDIDATES = 'candidates'
    QUEUED = 'queued'
    CAPTURING = 'capturing'
    REVIEW = 'review'
    INDEXING = 'indexing'
    SAVED = 'saved'
    HISTORY = 'history'


REVIEW_STAGES = {
    'awaiting_review': Stage.REVIEW,
    'publication_requested': Stage.INDEXING,
    'published_waiting_index': Stage.INDEXING,
    'indexing': Stage.INDEXING,
    'index_failed': Stage.INDEXING,
    'indexed': Stage.HISTORY,
    'indexing_declined': Stage.HISTORY,
}
CANDIDATE_STAGES = {
    'approved_waiting_batch': Stage.QUEUED,
    'capturing': Stage.CAPTURING,
    'captured_awaiting_review': Stage.REVIEW,
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
    states = dict(store.db.execute('SELECT id,state FROM batches'))
    for row in rows:
        capture = (row.get('coverage') or {}).get('capture') or {}
        review_id = capture.get('review_id') or capture.get('batch_id')
        if review_id in states:
            row['review_state'] = states[review_id]
        row['stage'] = REVIEW_STAGES.get(row.get('review_state'),
                         CANDIDATE_STAGES.get(row['state'], Stage.CANDIDATES)).value
    return rows


def counts(rows):
    return {stage.value: sum(row['stage'] == stage.value for row in rows) for stage in Stage}


def search_matches(row, query):
    values = [row['url'], row['scope'], (row.get('rating') or {}).get('category', '')]
    values.extend(capture.get('title', '') for capture in row.get('captures') or [])
    return query.casefold() in ' '.join(values).casefold()
