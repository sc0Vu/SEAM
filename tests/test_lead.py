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
