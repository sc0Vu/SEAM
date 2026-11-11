import json

import pytest

from coordinator.lead import Budget, GoalAlreadyLoaded, Lead, PlanError, load_plan
from engine.states import State
from engine.task import TaskSpec
from store import TaskStore


def write_plan(tmp_path, tasks, goal="g"):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"goal": goal, "tasks": tasks}))
    return p


def t(key, scope=("x/**",), deps=(), **kw):
    return {"key": key, "type": "backend", "description": key,
            "file_scope": list(scope), "blocked_by": list(deps), **kw}


def test_plan_is_ordered_dependencies_first_and_ids_resolved(tmp_path):
    plan = load_plan(write_plan(tmp_path, [t("c", deps=["b"]), t("a"), t("b", deps=["a"])]))
    assert [x["key"] for x in plan.tasks] == ["a", "b", "c"]

    store = TaskStore()
    created = Lead(store, "g1", Budget(1.0)).submit_plan(plan)
    assert created["b"].spec.blocked_by == (created["a"].id,)
    assert created["c"].spec.blocked_by == (created["b"].id,)
    assert all(x.state is State.OPEN and x.spec.goal_id == "g1" for x in created.values())


@pytest.mark.parametrize(
    "tasks, msg",
    [
        ([], "non-empty"),
        ([t("a"), t("a")], "unique"),
        ([t("a", deps=["zzz"])], "unknown"),
        ([t("a", deps=["b"]), t("b", deps=["a"])], "cycle"),
        ([t("a", scope=())], "file_scope"),
    ],
)
def test_bad_plans_rejected(tmp_path, tasks, msg):
    with pytest.raises(PlanError, match=msg):
        load_plan(write_plan(tmp_path, tasks))


def test_unreadable_plan_is_a_plan_error(tmp_path):
    with pytest.raises(PlanError):
        load_plan(tmp_path / "missing.json")


def test_budget_stops_dispatch_at_ceiling():
    lead = Lead(TaskStore(), "g", Budget(limit_usd=1.0))
    assert lead.can_dispatch()
    lead.budget.charge(0.6)
    assert lead.can_dispatch()
    lead.budget.charge(0.4)
    assert not lead.can_dispatch()


def test_loading_a_goal_twice_is_refused_and_creates_nothing(tmp_path):
    plan = load_plan(write_plan(tmp_path, [t("a"), t("b", scope=("y/**",))]))
    store = TaskStore()
    Lead(store, "g1", Budget(1.0)).submit_plan(plan)
    with pytest.raises(GoalAlreadyLoaded, match=r"\[1, 2\]"):
        Lead(store, "g1", Budget(1.0)).submit_plan(plan)
    assert len(store.list()) == 2
    Lead(store, "g2", Budget(1.0)).submit_plan(plan)  # another goal is fine
    assert len(store.list()) == 4


def test_goal_budget_starts_from_recorded_spend():
    from coordinator.lead import goal_budget
    from engine.usage import RunRecord, Usage

    store = TaskStore()
    t = store.create(TaskSpec(goal_id="g", type="backend", description="d",
                              file_scope=("a/**",)))
    store.record_run(RunRecord(t.id, "worker", "w", "diff", 0.75, Usage()))
    budget = goal_budget(store, 1.0, ["g"])
    assert budget.spent_usd == 0.75 and not budget.exhausted
    budget.charge(0.25)
    assert budget.exhausted  # a restarted run cannot spend the ceiling twice


# -- amend: a change of direction ---------------------------------------------------

def _plan(goal, *keys, deps=None):
    from coordinator.lead import plan_from_dict

    deps = deps or {}
    return plan_from_dict({"goal": goal, "tasks": [
        {"key": k, "type": "backend", "description": k, "file_scope": [f"{goal}/{k}.py"],
         "blocked_by": deps.get(k, [])} for k in keys]})


def _old_goal(store):
    """Goal A: `done` merged, `busy` in progress, `todo` open, `after` blocked by todo."""
    from engine.states import Event

    a = Lead(store, "A", Budget(1)).submit_plan(
        _plan("A", "done", "busy", "todo", "after", deps={"after": ["todo"]}))
    for key in ("done", "busy"):
        store.claim("w", "backend")
        store.transition(a[key].id, Event.START)
    d = a["done"].id
    for e in (Event.SUBMIT, Event.PICKUP, Event.APPROVE, Event.ENQUEUE, Event.TAKE,
              Event.MERGE_CLEAN):
        store.transition(d, e)
    return a


def test_amend_cancels_unstarted_keeps_the_rest_and_links_the_goals():
    store = TaskStore()
    a = _old_goal(store)
    cancelled, created = Lead(store, "B", Budget(1)).amend("A", _plan("B", "new"))
    assert sorted(t.id for t in cancelled) == [a["todo"].id, a["after"].id]
    assert store.get(a["done"].id).state is State.MERGED
    assert store.get(a["busy"].id).state is State.IN_PROGRESS  # finishes first
    assert store.get(created["new"].id).state is State.OPEN
    assert store.amendments() == [("B", "A")]
    assert store.goal_lineage("B") == ["B", "A"]


def test_amend_can_cancel_in_flight_work_too():
    store = TaskStore()
    a = _old_goal(store)
    cancelled, _ = Lead(store, "B", Budget(1)).amend("A", _plan("B", "new"),
                                                      cancel_in_flight=True)
    assert a["busy"].id in {t.id for t in cancelled}
    assert store.get(a["done"].id).state is State.MERGED  # merged work is never undone


@pytest.mark.parametrize("old, new, why", [
    ("nope", "B", "no goal 'nope'"),
    ("A", "A", "needs a new goal id"),
    ("A", "A2", "already loaded"),
])
def test_a_bad_amendment_changes_nothing(old, new, why):
    store = TaskStore()
    _old_goal(store)
    Lead(store, "A2", Budget(1)).submit_plan(_plan("A2", "x"))
    before = [(t.id, t.state) for t in store.list()]
    with pytest.raises(PlanError, match=why):
        Lead(store, new, Budget(1)).amend(old, _plan(new, "y"))
    assert [(t.id, t.state) for t in store.list()] == before
    assert store.amendments() == []


def test_amending_does_not_reset_the_budget():
    from coordinator.lead import goal_budget
    from engine.usage import RunRecord, Usage

    store = TaskStore()
    a = _old_goal(store)
    store.record_run(RunRecord(a["done"].id, "worker", "w", "diff", 0.6, Usage()))
    Lead(store, "B", Budget(1)).amend("A", _plan("B", "b1"))
    b1 = [t for t in store.list() if t.spec.goal_id == "B"][0]
    store.record_run(RunRecord(b1.id, "worker", "w", "diff", 0.3, Usage()))
    Lead(store, "C", Budget(1)).amend("B", _plan("C", "c1"))
    assert goal_budget(store, 1.0, ["C"]).spent_usd == pytest.approx(0.9)  # C+B+A
    assert goal_budget(store, 1.0, ["B"]).spent_usd == pytest.approx(0.9)  # B+A
    assert goal_budget(store, 1.0, ["A"]).spent_usd == pytest.approx(0.6)
