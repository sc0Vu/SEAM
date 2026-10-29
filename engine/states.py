"""The task state machine (doc/architecture.md §3).

Pure functions over states and events: no I/O, no store, no agent runtime. The store
applies these transitions; this module decides which ones are legal.
"""

from __future__ import annotations

from enum import StrEnum

from engine.errors import IllegalTransition

DEFAULT_MAX_ATTEMPTS = 3


class State(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    SCOPE_REQUESTED = "scope_requested"
    BLOCKED = "blocked"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    QUEUED_FOR_MERGE = "queued_for_merge"
    MERGING = "merging"
    MERGED = "merged"
    NEEDS_HUMAN = "needs_human"
    CANCELLED = "cancelled"


class Event(StrEnum):
    CLAIM = "claim"
    START = "start"
    REQUEST_SCOPE = "request_scope"
    GRANT_SCOPE = "grant_scope"
    DENY_SCOPE = "deny_scope"
    SUBMIT = "submit"
    FAIL = "fail"
    PICKUP = "pickup"
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    ENQUEUE = "enqueue"
    TAKE = "take"
    MERGE_CLEAN = "merge_clean"
    MERGE_CONFLICT = "merge_conflict"
    RESOLVE = "resolve"
    REWORK = "rework"
    UNBLOCK = "unblock"
    RECOVER = "recover"
    CANCEL = "cancel"


TERMINAL: frozenset[State] = frozenset({State.MERGED, State.CANCELLED})

# States whose task owns a branch with live edits. Its file_scope stays reserved so
# no other task can claim an overlapping scope (invariant 2). Every state except
# `open` (no edits yet) and the terminal ones. `blocked` and `needs_human` hold their
# scope on purpose: their branch still exists and may yet merge.
SCOPE_HOLDING: frozenset[State] = frozenset(State) - TERMINAL - {State.OPEN}

_TRANSITIONS: dict[tuple[State, Event], State] = {
    (State.OPEN, Event.CLAIM): State.CLAIMED,
    (State.CLAIMED, Event.START): State.IN_PROGRESS,
    (State.IN_PROGRESS, Event.REQUEST_SCOPE): State.SCOPE_REQUESTED,
    (State.SCOPE_REQUESTED, Event.GRANT_SCOPE): State.IN_PROGRESS,
    (State.SCOPE_REQUESTED, Event.DENY_SCOPE): State.BLOCKED,
    (State.BLOCKED, Event.UNBLOCK): State.IN_PROGRESS,
    (State.IN_PROGRESS, Event.SUBMIT): State.SUBMITTED,
    (State.IN_PROGRESS, Event.FAIL): State.IN_PROGRESS,
    (State.SUBMITTED, Event.PICKUP): State.IN_REVIEW,
    (State.IN_REVIEW, Event.APPROVE): State.APPROVED,
    (State.IN_REVIEW, Event.REQUEST_CHANGES): State.IN_PROGRESS,
    (State.IN_REVIEW, Event.FAIL): State.SUBMITTED,
    (State.APPROVED, Event.ENQUEUE): State.QUEUED_FOR_MERGE,
    (State.QUEUED_FOR_MERGE, Event.TAKE): State.MERGING,
    (State.MERGING, Event.MERGE_CLEAN): State.MERGED,
    (State.MERGING, Event.MERGE_CONFLICT): State.NEEDS_HUMAN,
    (State.NEEDS_HUMAN, Event.RESOLVE): State.QUEUED_FOR_MERGE,
    (State.NEEDS_HUMAN, Event.REWORK): State.IN_PROGRESS,
    (State.MERGING, Event.RECOVER): State.QUEUED_FOR_MERGE,
}


def next_state(
    state: State, event: Event, attempts: int, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> tuple[State, int]:
    """Return `(new_state, new_attempts)` for applying `event` to a task in `state`.

    Upholds the state machine's invariants:
    - Only listed transitions are legal; anything else raises `IllegalTransition`,
      so `merged` is reachable only via in_review -> ... -> merging (invariant 5).
    - `request_changes` counts an attempt; reaching `max_attempts` routes to
      `needs_human` instead of back to `in_progress` (invariant 11, bounded retries).
    - `fail` (a worker or review run crashed, or a worker produced nothing) counts an
      attempt the same way, so a run that keeps failing escalates instead of being
      retried forever. A failed review returns the task to `submitted`, not to the
      worker: the branch was never judged, so it is not the worker's to fix.
    - `request_scope` counts an attempt too: each grant costs another worker run, so
      a task that keeps asking for more files escalates instead of growing its scope
      (and spending budget) without bound.
    - `merge_conflict` also counts an attempt, for the record (it always escalates).
    - `rework` (a human sends an escalated task back to its branch) resets attempts:
      the human's judgment starts a fresh retry budget.
    - `cancel` is legal from any non-terminal state except `merging`: a merge in flight
      must finish or be recovered, never abandoned half-applied (invariant 12).
    """
    if event is Event.CANCEL:
        if state in TERMINAL or state is State.MERGING:
            raise IllegalTransition(state, event)
        return State.CANCELLED, attempts

    target = _TRANSITIONS.get((state, event))
    if target is None:
        raise IllegalTransition(state, event)

    if event is Event.REWORK:
        return target, 0
    if event in (Event.REQUEST_CHANGES, Event.FAIL, Event.REQUEST_SCOPE, Event.MERGE_CONFLICT):
        attempts += 1
        if attempts >= max_attempts:
            return State.NEEDS_HUMAN, attempts
    return target, attempts
