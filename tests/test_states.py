import pytest

from engine.errors import IllegalTransition
from engine.states import SCOPE_HOLDING, Event, State, next_state


def walk(events, state=State.OPEN, attempts=0, max_attempts=3):
    for e in events:
        state, attempts = next_state(state, e, attempts, max_attempts)
    return state, attempts


HAPPY = [
    Event.CLAIM, Event.START, Event.SUBMIT, Event.PICKUP, Event.APPROVE,
    Event.ENQUEUE, Event.TAKE, Event.MERGE_CLEAN,
]


def test_happy_path_reaches_merged_with_no_attempts_spent():
    assert walk(HAPPY) == (State.MERGED, 0)


def test_merged_unreachable_without_review():
    # Skipping review: submitted -> approve/enqueue/take must all be illegal.
    state, _ = walk([Event.CLAIM, Event.START, Event.SUBMIT])
    for e in (Event.APPROVE, Event.ENQUEUE, Event.TAKE, Event.MERGE_CLEAN):
        with pytest.raises(IllegalTransition):
            next_state(state, e, 0)


def test_changes_requested_returns_to_in_progress_until_bound():
    review = [Event.SUBMIT, Event.PICKUP, Event.REQUEST_CHANGES]
    state, attempts = walk([Event.CLAIM, Event.START, *review, *review])
    assert (state, attempts) == (State.IN_PROGRESS, 2)
    # Third rejection hits the default bound of 3 -> escalate, never loop.
    assert walk(review, state, attempts) == (State.NEEDS_HUMAN, 3)


def test_retry_bound_is_configurable():
    state, _ = walk([Event.CLAIM, Event.START, Event.SUBMIT, Event.PICKUP])
    assert next_state(state, Event.REQUEST_CHANGES, 0, max_attempts=1)[0] is State.NEEDS_HUMAN


def test_merge_conflict_escalates_and_human_resolve_requeues():
    state, attempts = walk(HAPPY[:-1] + [Event.MERGE_CONFLICT])
    assert state is State.NEEDS_HUMAN
    assert next_state(state, Event.RESOLVE, attempts)[0] is State.QUEUED_FOR_MERGE


def test_scope_request_grant_and_deny():
    state, _ = walk([Event.CLAIM, Event.START, Event.REQUEST_SCOPE])
    assert state is State.SCOPE_REQUESTED
    assert next_state(state, Event.GRANT_SCOPE, 0)[0] is State.IN_PROGRESS
    assert next_state(state, Event.DENY_SCOPE, 0)[0] is State.BLOCKED


def test_crash_recovery_requeues_merging():
    assert next_state(State.MERGING, Event.RECOVER, 0)[0] is State.QUEUED_FOR_MERGE


def test_cancel_allowed_except_terminal_and_mid_merge():
    assert next_state(State.IN_REVIEW, Event.CANCEL, 0)[0] is State.CANCELLED
    for s in (State.MERGING, State.MERGED, State.CANCELLED):
        with pytest.raises(IllegalTransition):
            next_state(s, Event.CANCEL, 0)


def test_scope_holding_covers_every_state_with_a_live_branch():
    expected = {
        State.CLAIMED, State.IN_PROGRESS, State.SCOPE_REQUESTED, State.BLOCKED,
        State.SUBMITTED, State.IN_REVIEW, State.APPROVED, State.QUEUED_FOR_MERGE,
        State.MERGING, State.NEEDS_HUMAN,
    }
    assert SCOPE_HOLDING == expected


def test_blocked_can_resume_or_cancel():
    assert next_state(State.BLOCKED, Event.UNBLOCK, 1)[0] is State.IN_PROGRESS
    assert next_state(State.BLOCKED, Event.CANCEL, 1)[0] is State.CANCELLED


def test_rework_from_needs_human_resets_retry_budget():
    review = [Event.SUBMIT, Event.PICKUP, Event.REQUEST_CHANGES]
    state, attempts = walk([Event.CLAIM, Event.START, *review * 3])
    assert (state, attempts) == (State.NEEDS_HUMAN, 3)
    assert next_state(state, Event.REWORK, attempts) == (State.IN_PROGRESS, 0)


def test_rework_only_from_needs_human():
    with pytest.raises(IllegalTransition):
        next_state(State.IN_REVIEW, Event.REWORK, 0)


def test_failed_worker_runs_are_bounded():
    state, attempts = walk([Event.CLAIM, Event.START, Event.FAIL, Event.FAIL])
    assert (state, attempts) == (State.IN_PROGRESS, 2)
    assert next_state(state, Event.FAIL, attempts) == (State.NEEDS_HUMAN, 3)
