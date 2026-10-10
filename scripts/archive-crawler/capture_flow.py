"""Typed candidate transitions shared by review and the capture worker."""
from enum import Enum

from common import CrawlError

UNDO_SECONDS = 60


class CandidateState(str, Enum):
    SUGGESTED = 'approval_pending'
    # Retain the persisted value for compatibility with existing operator state.
    AWAITING_CAPTURE = 'approved_waiting_batch'
    CAPTURING = 'capturing'
    RESUME_QUEUED = 'capture_resume_queued'
    CAPTURED = 'captured_awaiting_review'
    INDEX_APPROVED = 'approved_waiting_publication'
    INDEX_DECLINED = 'indexing_declined'
    PUBLISHED = 'published'
    INDEXED = 'indexed'
    DEFERRED = 'deferred'
    REJECTED = 'rejected'


class Action(str, Enum):
    APPROVE = 'approve'
    DEFER = 'defer'
    REJECT = 'reject'
    UNDO = 'undo'
    START = 'start_capture'
    QUEUE_RESUME = 'queue_capture_resume'
    AUTO_QUEUE_RESUME = 'automatic_capture_retry'
    RESUME = 'resume_capture'
    CANCEL_RESUME = 'cancel_capture_resume'
    COMPLETE = 'capture_complete'
    CONTINUE = 'continue_capture'
    UNDO_CONTINUE = 'undo_capture_continuation'
    RETRY_PUBLISHED_CAPTURE = 'retry_published_capture'
    UNDO_PUBLISHED_CAPTURE = 'undo_published_capture_retry'
    UNDO_INDEXED_CAPTURE = 'undo_indexed_capture_retry'
    APPROVE_INDEX = 'approve_indexing'
    DECLINE_INDEX = 'decline_indexing'
    RECONSIDER_INDEX = 'reconsider_indexing'
    RESTORE = 'restore_candidate'
    PUBLISH = 'publish'
    INDEX = 'index'


REVIEWABLE = frozenset(('approval_pending', 'approved_waiting_batch', 'deferred', 'rejected',
                        'discovered', 'sampled', 'sample_error', 'unavailable', 'identity_unresolved'))
TRANSITIONS = {
    Action.RESTORE: ({CandidateState.DEFERRED, CandidateState.REJECTED}, CandidateState.SUGGESTED),
    Action.APPROVE: (REVIEWABLE, CandidateState.AWAITING_CAPTURE),
    Action.REJECT: (REVIEWABLE | {'grade_error', 'coverage_unverified'}, CandidateState.REJECTED),
    Action.DEFER: (REVIEWABLE | {'grade_error', 'coverage_unverified'}, CandidateState.DEFERRED),
    Action.UNDO: ({CandidateState.AWAITING_CAPTURE}, CandidateState.SUGGESTED),
    Action.START: ({CandidateState.AWAITING_CAPTURE}, CandidateState.CAPTURING),
    Action.QUEUE_RESUME: ({CandidateState.CAPTURING}, CandidateState.RESUME_QUEUED),
    Action.AUTO_QUEUE_RESUME: ({CandidateState.CAPTURING}, CandidateState.RESUME_QUEUED),
    Action.RESUME: ({CandidateState.RESUME_QUEUED}, CandidateState.CAPTURING),
    Action.CANCEL_RESUME: ({CandidateState.RESUME_QUEUED}, CandidateState.CAPTURING),
    Action.COMPLETE: ({CandidateState.CAPTURING}, CandidateState.CAPTURED),
    Action.CONTINUE: ({CandidateState.CAPTURED}, CandidateState.AWAITING_CAPTURE),
    Action.UNDO_CONTINUE: ({CandidateState.AWAITING_CAPTURE}, CandidateState.CAPTURED),
    Action.RETRY_PUBLISHED_CAPTURE: ({CandidateState.PUBLISHED, CandidateState.INDEXED}, CandidateState.AWAITING_CAPTURE),
    Action.UNDO_PUBLISHED_CAPTURE: ({CandidateState.AWAITING_CAPTURE}, CandidateState.PUBLISHED),
    Action.UNDO_INDEXED_CAPTURE: ({CandidateState.AWAITING_CAPTURE}, CandidateState.INDEXED),
    Action.APPROVE_INDEX: ({CandidateState.CAPTURED}, CandidateState.INDEX_APPROVED),
    Action.DECLINE_INDEX: ({CandidateState.CAPTURED}, CandidateState.INDEX_DECLINED),
    Action.RECONSIDER_INDEX: ({CandidateState.INDEX_DECLINED}, CandidateState.CAPTURED),
    Action.PUBLISH: ({CandidateState.CAPTURED, CandidateState.INDEX_APPROVED}, CandidateState.PUBLISHED),
    Action.INDEX: ({CandidateState.PUBLISHED}, CandidateState.INDEXED),
}


def transition(state, action):
    allowed, target = TRANSITIONS[Action(action)]
    if state not in allowed:
        raise CrawlError(f'Cannot {action.value if isinstance(action, Action) else action} while candidate is {state}')
    return target.value
