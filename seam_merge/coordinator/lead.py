"""The lead: sole task producer, scope arbiter, and budget owner (invariant 7).

v1 takes a hand-written plan (a JSON task list — doc §8 allows a hardcoded lead);
LLM goal decomposition comes later behind the same `Lead` surface.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from seam_merge.engine.errors import SeamError
from seam_merge.engine.states import TERMINAL, Event, State
from seam_merge.engine.task import Task, TaskSpec
from seam_merge.store.sqlite import TaskStore


class PlanError(SeamError):
    """The plan is malformed: missing fields, unknown or cyclic dependencies."""


class GoalAlreadyLoaded(PlanError):
    """The store already holds this goal's tasks; loading again would duplicate them."""


@dataclass
class Budget:
    """A goal's dollar ceiling. Spend is charged as agent runs report their cost."""

    limit_usd: float
    spent_usd: float = 0.0

    def charge(self, cost_usd: float) -> None:
        self.spent_usd += cost_usd

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd


def goal_budget(store: TaskStore, limit_usd: float, goal_ids: Iterable[str]) -> Budget:
    """The budget for a run of these goals, starting from what earlier runs already
    spent on them (from the `runs` table), so restarting `run` never resets the cap.
    It includes the goals each one amends, so amending a goal doesn't reset it either."""
    lineage = {g for goal in goal_ids for g in store.goal_lineage(goal)}
    return Budget(limit_usd, store.spent_usd(sorted(lineage)))


@dataclass(frozen=True)
class Plan:
    goal: str
    tasks: list[dict]


def load_plan(path: str | Path) -> Plan:
    """Parse and validate a plan file. See examples/greeter/*.json for the format."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise PlanError(f"cannot read plan {path}: {e}") from e
    return plan_from_dict(data)


def plan_from_dict(data: object) -> Plan:
    """Validate a plan however it was produced (a file, or an LLM planner): the same
    checks apply, so a generated plan is held to the hand-written plan's rules."""
    if not isinstance(data, dict):
        raise PlanError("a plan must be a JSON object")
    goal, tasks = data.get("goal"), data.get("tasks")
    if not goal or not isinstance(tasks, list) or not tasks:
        raise PlanError("plan needs a 'goal' and a non-empty 'tasks' list")
    if not all(isinstance(t, dict) for t in tasks):
        raise PlanError("every task must be a JSON object")
    keys = [t.get("key") for t in tasks]
    if None in keys or len(set(keys)) != len(keys):
        raise PlanError("every task needs a unique 'key'")
    for t in tasks:
        for field in ("type", "description", "file_scope"):
            if not t.get(field):
                raise PlanError(f"task {t['key']!r} is missing {field!r}")
        unknown = set(t.get("blocked_by", [])) - set(keys)
        if unknown:
            raise PlanError(f"task {t['key']!r} depends on unknown tasks {sorted(unknown)}")
        allowed = t.get("may_change_tests", [])
        if not isinstance(allowed, list) or not all(
                isinstance(x, str) and "::" in x and x.split("::")[-1] for x in allowed):
            raise PlanError(f"task {t['key']!r}: may_change_tests must be a list of "
                            "test ids like 'tests/test_x.py::test_name'")
    return Plan(goal=goal, tasks=_topological(tasks))


def _topological(tasks: list[dict]) -> list[dict]:
    by_key = {t["key"]: t for t in tasks}
    done: dict[str, None] = {}
    visiting: set[str] = set()

    def visit(key: str) -> None:
        if key in done:
            return
        if key in visiting:
            raise PlanError(f"dependency cycle through task {key!r}")
        visiting.add(key)
        for dep in by_key[key].get("blocked_by", []):
            visit(dep)
        visiting.discard(key)
        done[key] = None

    for t in tasks:
        visit(t["key"])
    return [by_key[k] for k in done]


class Lead:
    """Turns a plan into tasks, rules on scope requests, and enforces the budget cap."""

    def __init__(self, store: TaskStore, goal_id: str, budget: Budget) -> None:
        self.store = store
        self.goal_id = goal_id
        self.budget = budget

    def submit_plan(self, plan: Plan) -> dict[str, Task]:
        """Create every planned task (dependencies first); return them by plan key.

        A goal is loaded once: if the store already has tasks for it, this raises
        `GoalAlreadyLoaded` instead of creating duplicates, which would hold the same
        file_scopes and leave the copies unclaimable. (Check-then-create is safe
        because the orchestrator process is the store's only writer.)
        """
        existing = [t.id for t in self.store.list() if t.spec.goal_id == self.goal_id]
        if existing:
            raise GoalAlreadyLoaded(
                f"goal {self.goal_id!r} is already loaded as tasks {existing}; resume with "
                "`run` without --plan, or use a fresh --state-dir or a new --goal")
        self.store.record_goal(self.goal_id)
        created: dict[str, Task] = {}
        for t in plan.tasks:
            created[t["key"]] = self.store.create(TaskSpec(
                goal_id=self.goal_id,
                type=t["type"],
                description=t["description"],
                file_scope=tuple(t["file_scope"]),
                spec_ref=t.get("spec_ref", ""),
                blocked_by=tuple(created[d].id for d in t.get("blocked_by", [])),
                priority=t.get("priority", 0),
                test_cmd=t.get("test_cmd", ""),
                may_change_tests=tuple(t.get("may_change_tests", [])),
            ))
        return created

    def amend(self, old_goal: str, plan: Plan,
              cancel_in_flight: bool = False) -> tuple[list[Task], dict[str, Task]]:
        """Change direction: replace `old_goal` with this lead's goal and `plan`.

        Cancels the old goal's unstarted (`open`) tasks, and with `cancel_in_flight`
        every other cancellable one too (never one that is `merging`: invariant 12).
        Merged work stays on main, and in-flight tasks that are kept still hold their
        scopes, so new tasks touching the same files wait for them (invariant 2).
        Then loads the new plan and records that this goal amends `old_goal`, which
        carries the old goal's spend into this goal's budget (`goal_budget`).

        Everything is checked before anything changes: an unknown old goal, a new goal
        already loaded, or reusing the old goal's id raise `PlanError` with the store
        untouched. Returns (cancelled tasks, created tasks by plan key).
        """
        old = [t for t in self.store.list() if t.spec.goal_id == old_goal]
        if not old:
            raise PlanError(f"no goal {old_goal!r} in this state dir")
        if self.goal_id == old_goal:
            raise PlanError("an amendment needs a new goal id (set --goal)")
        if any(t.spec.goal_id == self.goal_id for t in self.store.list()):
            raise GoalAlreadyLoaded(f"goal {self.goal_id!r} is already loaded")
        drop = {State.OPEN} if not cancel_in_flight else \
            set(State) - TERMINAL - {State.MERGING}
        cancelled: dict[int, Task] = {}
        for t in old:
            if self.store.get(t.id).state in drop:  # a cascade may have cancelled it
                before = {x.id for x in self.store.list(State.CANCELLED)}
                self.store.transition(t.id, Event.CANCEL)
                for x in self.store.list(State.CANCELLED):
                    if x.id not in before:
                        cancelled[x.id] = x
        self.store.record_goal(old_goal)
        self.store.record_goal(self.goal_id, supersedes=old_goal)
        created = self.submit_plan(plan)
        return list(cancelled.values()), created

    def decide_scope(self, task_id: int, globs: list[str]) -> Task:
        """Grant a scope expansion iff it keeps every in-flight scope disjoint."""
        return self.store.grant_scope(task_id, globs)

    def can_dispatch(self) -> bool:
        """False once the goal's budget is spent: no new agent runs after that."""
        return not self.budget.exhausted
