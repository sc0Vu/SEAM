"""The lead: sole task producer, scope arbiter, and budget owner (invariant 7).

v1 takes a hand-written plan (a JSON task list — doc §8 allows a hardcoded lead);
LLM goal decomposition comes later behind the same `Lead` surface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from engine.errors import SeamError
from engine.task import Task, TaskSpec
from store.sqlite import TaskStore


class PlanError(SeamError):
    """The plan is malformed: missing fields, unknown or cyclic dependencies."""


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
    goal, tasks = data.get("goal"), data.get("tasks")
    if not goal or not isinstance(tasks, list) or not tasks:
        raise PlanError("plan needs a 'goal' and a non-empty 'tasks' list")
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
        """Create every planned task (dependencies first); return them by plan key."""
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
            ))
        return created

    def decide_scope(self, task_id: int, globs: list[str]) -> Task:
        """Grant a scope expansion iff it keeps every in-flight scope disjoint."""
        return self.store.grant_scope(task_id, globs)

    def can_dispatch(self) -> bool:
        """False once the goal's budget is spent: no new agent runs after that."""
        return not self.budget.exhausted
