"""What a plan revision (`seam plan --base`) must leave out: the base's merged work
(doc §12, "Revising a plan").

A base plan whose goal already ran is partly done. Handing it to the planner as is
would have it keep the merged tasks, and `amend` would load and run them a second
time. So the base is trimmed against the task store first: merged tasks come out
(their code is on main, which the planner reads), and a revision while tasks are
still in flight is refused, since a kept task that depends on one of them would lose
that dependency (a missing `blocked_by` is how two green tasks break main).
Plain Python over the store; no runtime imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from seam_merge.coordinator.lead import Plan, PlanError
from seam_merge.engine.states import SCOPE_HOLDING, State
from seam_merge.engine.task import Task

# Started and not finished: its result is not known yet.
IN_FLIGHT = SCOPE_HOLDING - {State.NEEDS_HUMAN}


class BaseInFlight(PlanError):
    """The base plan's goal still has tasks running; revise it once they finish."""


@dataclass(frozen=True)
class BaseTask:
    """A base task the store knows: its plan key, task id, and state."""

    task: str
    task_id: int
    state: str


@dataclass(frozen=True)
class TrimmedBase:
    """The base to give the planner, and what was found in the store.

    `merged`: left out of `base`; never planned again. `escalated`: kept in `base`,
    but the old task still holds its files, so a copy in the revision waits until a
    human cancels it (or `amend --cancel-in-flight` does)."""

    base: Plan
    merged: list[BaseTask] = field(default_factory=list)
    escalated: list[BaseTask] = field(default_factory=list)


def trim_base(base: Plan, goal_tasks: list[Task]) -> TrimmedBase:
    """Trim `base` against the stored tasks of the goal it was loaded as.

    No stored tasks: the base is unchanged. Tasks stored without plan keys (an older
    state dir) can't be matched to the base, and in-flight tasks have no outcome yet:
    both raise (`PlanError`, `BaseInFlight`) rather than guess. Merged tasks are
    removed, along with every `blocked_by` reference to them (their code is already
    on main). Stored tasks whose key the base no longer has are ignored: the human
    took them out."""
    if not goal_tasks:
        return TrimmedBase(base)
    unkeyed = [t.id for t in goal_tasks if t.spec.plan_key is None]
    if unkeyed:
        raise PlanError(f"tasks {unkeyed} of goal {goal_tasks[0].spec.goal_id!r} were made "
                        "before SEAM recorded plan keys: remove the merged tasks from the "
                        "base by hand, and pass a state dir without them")
    running = [f"#{t.id} {t.spec.plan_key} ({t.state})" for t in goal_tasks
               if t.state in IN_FLIGHT]
    if running:
        raise BaseInFlight(f"goal {goal_tasks[0].spec.goal_id!r} still has tasks running: "
                           f"{', '.join(running)}; revise it once they finish")
    keys = {t["key"] for t in base.tasks}
    stored = {t.spec.plan_key: t for t in goal_tasks if t.spec.plan_key in keys}
    merged = [BaseTask(k, t.id, str(t.state)) for k, t in stored.items()
              if t.state is State.MERGED]
    escalated = [BaseTask(k, t.id, str(t.state)) for k, t in stored.items()
                 if t.state is State.NEEDS_HUMAN]
    gone = {m.task for m in merged}
    kept = [t if not gone & set(t.get("blocked_by", [])) else
            {**t, "blocked_by": [d for d in t["blocked_by"] if d not in gone]}
            for t in base.tasks if t["key"] not in gone]
    return TrimmedBase(Plan(base.goal, kept), merged, escalated)
