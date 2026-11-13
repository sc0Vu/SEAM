"""Trimming a base plan against the store before a revision (coordinator/revision.py),
and the CLI's lookup of the base's goal (cli.trim_against_store)."""

import pytest

from seam_merge import cli
from seam_merge.coordinator.lead import Budget, Lead, Plan, PlanError, plan_from_dict
from seam_merge.coordinator.revision import BaseInFlight, BaseTask, trim_base
from seam_merge.engine.states import Event, State
from seam_merge.engine.task import Task, TaskSpec
from seam_merge.store import TaskStore

# logic <- ui <- docs; pages independent.
BASE = plan_from_dict({"goal": "snow", "tasks": [
    {"key": "logic", "type": "frontend", "description": "logic", "file_scope": ["src/logic/**"]},
    {"key": "ui", "type": "frontend", "description": "ui", "file_scope": ["src/ui/**"],
     "blocked_by": ["logic"]},
    {"key": "docs", "type": "docs", "description": "docs", "file_scope": ["doc/**"],
     "blocked_by": ["logic", "ui"]},
    {"key": "pages", "type": "devops", "description": "pages", "file_scope": [".github/**"]},
]})


def task(id, key, state, goal="snow"):
    return Task(id, TaskSpec(goal, "frontend", key, ("x",), plan_key=key), State(state))


def keys(plan: Plan):
    return [t["key"] for t in plan.tasks]


def test_a_goal_never_run_leaves_the_base_as_it_is():
    trimmed = trim_base(BASE, [])
    assert trimmed.base == BASE and trimmed.merged == [] and trimmed.escalated == []


def test_merged_tasks_leave_the_base_and_their_dependency_edges_with_them():
    trimmed = trim_base(BASE, [task(1, "logic", "merged"), task(2, "ui", "needs_human"),
                               task(3, "docs", "open"), task(4, "pages", "merged")])
    assert keys(trimmed.base) == ["ui", "docs"]
    deps = {t["key"]: t["blocked_by"] for t in trimmed.base.tasks}
    assert deps == {"ui": [], "docs": ["ui"]}  # logic is on main; ui still pending
    assert trimmed.merged == [BaseTask("logic", 1, "merged"), BaseTask("pages", 4, "merged")]
    assert trimmed.escalated == [BaseTask("ui", 2, "needs_human")]
    # The result is still a valid plan once the planner returns it whole.
    plan_from_dict({"goal": "snow", "tasks": trimmed.base.tasks})


@pytest.mark.parametrize("state", ["claimed", "in_progress", "submitted", "in_review",
                                   "approved", "queued_for_merge", "merging", "blocked",
                                   "scope_requested"])
def test_a_goal_with_tasks_still_running_is_refused(state):
    with pytest.raises(BaseInFlight, match="still has tasks running"):
        trim_base(BASE, [task(1, "logic", "merged"), task(2, "ui", state)])


def test_cancelled_and_unknown_keys_are_kept_or_ignored_not_guessed():
    trimmed = trim_base(BASE, [task(1, "logic", "cancelled"), task(9, "gone", "merged")])
    assert trimmed.base == BASE  # cancelled stays for the human; "gone" isn't in the base
    assert trimmed.merged == []


def test_tasks_stored_without_plan_keys_are_refused():
    old = Task(1, TaskSpec("snow", "frontend", "logic", ("x",)), State.MERGED)
    with pytest.raises(PlanError, match="before SEAM recorded plan keys"):
        trim_base(BASE, [old])


def _merge(store, task_id):
    store.claim("w", "frontend")
    for e in (Event.START, Event.SUBMIT, Event.PICKUP, Event.APPROVE, Event.ENQUEUE,
              Event.TAKE, Event.MERGE_CLEAN):
        store.transition(task_id, e)


def test_cli_trims_against_the_goal_in_the_state_dir(tmp_path):
    assert cli.trim_against_store(tmp_path, BASE, None).base == BASE  # no state dir yet
    store = TaskStore(tmp_path / "seam.sqlite")
    created = Lead(store, "snow", Budget(1)).submit_plan(BASE)
    _merge(store, created["logic"].id)
    store.close()
    trimmed = cli.trim_against_store(tmp_path, BASE, None)
    assert keys(trimmed.base) == ["ui", "docs", "pages"]
    assert [m.task for m in trimmed.merged] == ["logic"]
    # Loaded under another id: only that id finds it.
    assert cli.trim_against_store(tmp_path, BASE, "other").base == BASE


def test_cli_refuses_an_amended_goal_unless_named(tmp_path):
    store = TaskStore(tmp_path / "seam.sqlite")
    Lead(store, "snow", Budget(1)).submit_plan(BASE)
    Lead(store, "snow2", Budget(1)).amend("snow", BASE)
    store.close()
    with pytest.raises(PlanError, match="amended by 'snow2'"):
        cli.trim_against_store(tmp_path, BASE, None)
    assert keys(cli.trim_against_store(tmp_path, BASE, "snow2").base) == keys(BASE)
