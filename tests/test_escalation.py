"""Human rulings on escalated tasks (coordinator/escalation.py)."""

import pytest

from coordinator import escalation
from coordinator.escalation import EscalationError
from engine.errors import IllegalTransition
from engine.states import Event, State
from engine.task import TaskSpec
from engine.usage import RunRecord, Usage
from store import TaskStore


@pytest.fixture
def store():
    s = TaskStore()
    yield s
    s.close()


def new(store, deps=()):
    return store.create(TaskSpec(goal_id="g", type="backend", description="d",
                                 file_scope=(f"f{len(store.list())}.py",),
                                 blocked_by=tuple(deps)))


def run(store, task_id, role, outcome):
    store.record_run(RunRecord(task_id, role, role, outcome, 0.1, Usage()))


def escalated_at_merge(store):
    """Approved by review, then the merge-time full suite failed."""
    t = new(store)
    store.claim("w1", "backend")
    store.transition(t.id, Event.START)
    run(store, t.id, "worker", "diff")
    store.transition(t.id, Event.SUBMIT)
    store.transition(t.id, Event.PICKUP)
    run(store, t.id, "reviewer", "approve")
    for e in (Event.APPROVE, Event.ENQUEUE, Event.TAKE, Event.MERGE_CONFLICT):
        store.transition(t.id, e)
    return store.get(t.id)


def escalated_by_review(store):
    """Rejected by review until the retry bound."""
    t = new(store)
    store.claim("w1", "backend")
    store.transition(t.id, Event.START)
    while store.get(t.id).state is not State.NEEDS_HUMAN:
        run(store, t.id, "worker", "diff")
        store.transition(t.id, Event.SUBMIT)
        store.transition(t.id, Event.PICKUP)
        run(store, t.id, "reviewer", "reject")
        store.transition(t.id, Event.REQUEST_CHANGES, review_notes="wrong")
    return store.get(t.id)


def test_resolve_requeues_an_approved_branch_and_records_the_ruling(store):
    t = escalated_at_merge(store)
    assert t.state is State.NEEDS_HUMAN
    done = escalation.resolve(store, t.id, "main was red; fixed upstream")
    assert done.state is State.QUEUED_FOR_MERGE
    last = store.runs(t.id)[-1]
    assert (last.role, last.outcome, last.notes, last.cost_usd) == (
        "human", "resolve", "main was red; fixed upstream", 0.0)


def test_resolve_refuses_a_branch_that_failed_review(store):
    """Invariant 5: resolve must never become a way to merge without an approving
    review."""
    t = escalated_by_review(store)
    with pytest.raises(EscalationError, match="no approving review"):
        escalation.resolve(store, t.id)
    assert store.get(t.id).state is State.NEEDS_HUMAN
    assert all(r.role != "human" for r in store.runs(t.id))  # nothing recorded


def test_a_worker_run_after_approval_voids_it(store):
    t = escalated_at_merge(store)
    escalation.rework(store, t.id, "also handle empty input")
    run(store, t.id, "worker", "diff")  # the branch changed after its approval
    assert not escalation.approved_as_is(store, t.id)


def test_human_rulings_do_not_void_an_approval(store):
    """resolve -> merge fails again -> resolve again: the branch is still the one the
    review approved, so the second resolve is allowed."""
    t = escalated_at_merge(store)
    escalation.resolve(store, t.id)
    store.transition(t.id, Event.TAKE)
    store.transition(t.id, Event.MERGE_CONFLICT)
    assert escalation.resolve(store, t.id).state is State.QUEUED_FOR_MERGE


def test_rulings_apply_only_to_escalated_tasks(store):
    t = new(store)
    for ruling in (escalation.resolve, escalation.rework):
        with pytest.raises(EscalationError, match="not needs_human"):
            ruling(store, t.id, "x")


def test_rework_needs_notes_and_restarts_the_retry_budget(store):
    t = escalated_by_review(store)
    with pytest.raises(EscalationError, match="needs notes"):
        escalation.rework(store, t.id, "  ")
    back = escalation.rework(store, t.id, "use the existing parser")
    assert (back.state, back.attempts, back.owner) == (State.IN_PROGRESS, 0, "w1")
    assert back.review_notes == "From a human: use the existing parser"
    assert store.runs(t.id)[-1].outcome == "rework"


def test_cancel_cascades_and_records(store):
    t = escalated_by_review(store)
    child = new(store, deps=(t.id,))
    other = new(store)
    gone = escalation.cancel(store, t.id, "not needed")
    assert sorted(x.id for x in gone) == [t.id, child.id]
    assert store.get(other.id).state is State.OPEN
    assert store.runs(t.id)[-1].outcome == "cancel"


def test_cancel_refuses_a_task_mid_merge(store):
    t = escalated_at_merge(store)
    escalation.resolve(store, t.id)
    store.transition(t.id, Event.TAKE)
    with pytest.raises(IllegalTransition):
        escalation.cancel(store, t.id)
