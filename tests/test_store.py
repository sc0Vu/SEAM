import pytest

from engine.errors import IllegalTransition, MergeSlotBusy, StaleState
from engine.states import Event, State
from engine.task import TaskSpec
from store import TaskStore


@pytest.fixture
def store():
    s = TaskStore()
    yield s
    s.close()


def spec(scope, type="backend", **kw):
    return TaskSpec(goal_id="g1", type=type, description="d", file_scope=tuple(scope), **kw)


def to_queued(store, task_id):
    for e in (Event.START, Event.SUBMIT, Event.PICKUP, Event.APPROVE, Event.ENQUEUE):
        store.transition(task_id, e)


def test_claim_respects_type_and_priority(store):
    store.create(spec(["a/**"], type="frontend"))
    low = store.create(spec(["b/**"], priority=1))
    high = store.create(spec(["c/**"], priority=5))
    got = store.claim("w1", "backend")
    assert (got.id, got.state, got.owner) == (high.id, State.CLAIMED, "w1")
    assert store.claim("w2", "backend").id == low.id
    assert store.claim("w3", "backend") is None


def test_claim_skips_overlapping_scope_while_holder_in_flight(store):
    first = store.create(spec(["auth/**"]))
    clash = store.create(spec(["auth/login.py"]))
    free = store.create(spec(["billing/**"]))
    assert store.claim("w1", "backend").id == first.id
    assert store.claim("w2", "backend").id == free.id
    assert store.claim("w3", "backend") is None  # clash stays open
    assert store.get(clash.id).state is State.OPEN


@pytest.mark.parametrize("hold_state_events", [
    [Event.START, Event.SUBMIT],                      # submitted
    [Event.START, Event.SUBMIT, Event.PICKUP, Event.APPROVE],  # approved
])
def test_scope_still_held_between_review_and_merge(store, hold_state_events):
    first = store.create(spec(["auth/**"]))
    store.create(spec(["auth/**"]))
    store.claim("w1", "backend")
    for e in hold_state_events:
        store.transition(first.id, e)
    assert store.claim("w2", "backend") is None


def test_scope_released_once_merged(store):
    first = store.create(spec(["auth/**"]))
    second = store.create(spec(["auth/**"]))
    store.claim("w1", "backend")
    to_queued(store, first.id)
    store.transition(first.id, Event.TAKE)
    store.transition(first.id, Event.MERGE_CLEAN)
    assert store.claim("w2", "backend").id == second.id


def test_claim_waits_for_dependencies_to_merge(store):
    dep = store.create(spec(["a/**"]))
    child = store.create(spec(["b/**"], blocked_by=(dep.id,)))
    assert store.claim("w1", "backend").id == dep.id
    assert store.claim("w2", "backend") is None
    to_queued(store, dep.id)
    store.transition(dep.id, Event.TAKE)
    store.transition(dep.id, Event.MERGE_CLEAN)
    assert store.claim("w2", "backend").id == child.id


def test_single_merge_slot(store):
    a = store.create(spec(["a/**"]))
    b = store.create(spec(["b/**"]))
    for t in (a, b):
        store.claim("w", "backend")
        to_queued(store, t.id)
    store.transition(a.id, Event.TAKE)
    with pytest.raises(MergeSlotBusy):
        store.transition(b.id, Event.TAKE)
    assert store.get(b.id).state is State.QUEUED_FOR_MERGE
    store.transition(a.id, Event.MERGE_CLEAN)
    assert store.transition(b.id, Event.TAKE).state is State.MERGING


def test_illegal_transition_leaves_task_unchanged(store):
    t = store.create(spec(["a/**"]))
    with pytest.raises(IllegalTransition):
        store.transition(t.id, Event.MERGE_CLEAN)
    assert store.get(t.id).state is State.OPEN


def test_review_bound_escalates_via_store():
    s = TaskStore(max_attempts=2)
    t = s.create(spec(["a/**"]))
    s.claim("w", "backend")
    s.transition(t.id, Event.START)
    for expected in (State.IN_PROGRESS, State.NEEDS_HUMAN):
        s.transition(t.id, Event.SUBMIT)
        s.transition(t.id, Event.PICKUP)
        got = s.transition(t.id, Event.REQUEST_CHANGES, review_notes="fix it")
        assert got.state is expected
    assert (got.attempts, got.review_notes) == (2, "fix it")
    s.close()


def test_grant_scope_checks_disjointness(store):
    a = store.create(spec(["a/**"]))
    store.create(spec(["b/**"]))
    store.claim("w1", "backend")
    store.claim("w2", "backend")
    store.transition(a.id, Event.START)

    store.transition(a.id, Event.REQUEST_SCOPE)
    granted = store.grant_scope(a.id, ["c/x.py"])
    assert (granted.state, granted.spec.file_scope) == (State.IN_PROGRESS, ("a/**", "c/x.py"))

    store.transition(a.id, Event.REQUEST_SCOPE)
    denied = store.grant_scope(a.id, ["b/y.py"])  # held by the other in-flight task
    assert (denied.state, denied.spec.file_scope) == (State.BLOCKED, ("a/**", "c/x.py"))


def crash_mid_merge(tmp_path, record):
    """Take a task into `merging`, optionally record its merge commit, then 'crash'."""
    db = tmp_path / "seam.sqlite"
    s = TaskStore(db)
    t = s.create(spec(["a/**"]))
    s.claim("w", "backend")
    to_queued(s, t.id)
    s.transition(t.id, Event.TAKE)
    if record:
        s.record_merge_commit(t.id, base_sha="base1", merge_sha="merge1")
    s.close()
    return TaskStore(db), t.id


def test_recover_requeues_when_merge_never_recorded(tmp_path):
    store, tid = crash_mid_merge(tmp_path, record=False)
    recovered = store.recover_merging(landed=lambda sha: True)
    assert [(r.id, r.state) for r in recovered] == [(tid, State.QUEUED_FOR_MERGE)]
    store.close()


def test_recover_requeues_when_recorded_merge_not_on_main(tmp_path):
    store, tid = crash_mid_merge(tmp_path, record=True)
    asked = []
    recovered = store.recover_merging(landed=lambda sha: asked.append(sha) or False)
    assert asked == ["merge1"]
    assert recovered[0].state is State.QUEUED_FOR_MERGE
    store.close()


def test_recover_finishes_merge_that_already_landed(tmp_path):
    store, tid = crash_mid_merge(tmp_path, record=True)
    recovered = store.recover_merging(landed=lambda sha: sha == "merge1")
    assert [(r.id, r.state) for r in recovered] == [(tid, State.MERGED)]
    store.close()


def test_retake_forgets_previous_merge_commit(tmp_path):
    store, tid = crash_mid_merge(tmp_path, record=True)
    store.recover_merging(landed=lambda sha: False)
    retaken = store.transition(tid, Event.TAKE)
    assert (retaken.base_sha, retaken.merge_sha) == (None, None)
    store.close()


def test_merge_commit_only_recorded_while_merging(store):
    t = store.create(spec(["a/**"]))
    with pytest.raises(StaleState):
        store.record_merge_commit(t.id, "b", "m")


def test_cancel_cascades_to_dependents_only(store):
    root = store.create(spec(["a/**"]))
    child = store.create(spec(["b/**"], blocked_by=(root.id,)))
    grandchild = store.create(spec(["c/**"], blocked_by=(child.id,)))
    unrelated = store.create(spec(["d/**"]))
    store.transition(root.id, Event.CANCEL)
    states = {t.id: t.state for t in store.list()}
    assert states == {
        root.id: State.CANCELLED,
        child.id: State.CANCELLED,
        grandchild.id: State.CANCELLED,
        unrelated.id: State.OPEN,
    }


def test_blocked_task_unblocks_once_scope_frees(store):
    a = store.create(spec(["a/**"]))
    b = store.create(spec(["b/**"]))
    store.claim("w1", "backend")
    store.claim("w2", "backend")
    store.transition(a.id, Event.START)
    store.transition(a.id, Event.REQUEST_SCOPE)
    assert store.grant_scope(a.id, ["b/y.py"]).state is State.BLOCKED

    # Retrying while b still holds b/** leaves a blocked and its scope unchanged.
    still = store.grant_scope(a.id, ["b/y.py"])
    assert (still.state, still.spec.file_scope) == (State.BLOCKED, ("a/**",))

    store.transition(b.id, Event.CANCEL)
    freed = store.grant_scope(a.id, ["b/y.py"])
    assert (freed.state, freed.spec.file_scope) == (State.IN_PROGRESS, ("a/**", "b/y.py"))
