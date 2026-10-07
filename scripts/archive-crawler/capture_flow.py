"""Typed candidate transitions shared by review and the capture worker."""
from enum import Enum

from common import CrawlError

UNDO_SECONDS = 60


class CandidateState(str, Enum):
    SUGGESTED = 'approval_pending'
    # Retain the persisted value for compatibility with existing operator state.
    AWAITING_CAPTURE = 'approved_waiting_batch'
    CAPTURING = 'capturing'
    CAPTURED = 'captured_awaiting_review'
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
    COMPLETE = 'capture_complete'
    PUBLISH = 'publish'
    INDEX = 'index'


REVIEWABLE = frozenset(('approval_pending', 'approved_waiting_batch', 'deferred', 'rejected',
                        'discovered', 'sampled', 'sample_error', 'unavailable', 'identity_unresolved'))
TRANSITIONS = {
    Action.APPROVE: (REVIEWABLE, CandidateState.AWAITING_CAPTURE),
    Action.REJECT: (REVIEWABLE, CandidateState.REJECTED),
    Action.DEFER: (REVIEWABLE, CandidateState.DEFERRED),
    Action.UNDO: ({CandidateState.AWAITING_CAPTURE}, CandidateState.SUGGESTED),
    Action.START: ({CandidateState.AWAITING_CAPTURE}, CandidateState.CAPTURING),
    Action.COMPLETE: ({CandidateState.CAPTURING}, CandidateState.CAPTURED),
    Action.PUBLISH: ({CandidateState.CAPTURED}, CandidateState.PUBLISHED),
    Action.INDEX: ({CandidateState.PUBLISHED}, CandidateState.INDEXED),
}


def transition(state, action):
    allowed, target = TRANSITIONS[Action(action)]
    if state not in allowed:
        raise CrawlError(f'Cannot {action.value if isinstance(action, Action) else action} while candidate is {state}')
    return target.value
