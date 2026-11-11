"""The human's decisions on escalated tasks (invariant 9: judgment lives with a human).

A task in `needs_human` stays there until a person rules on it. The three rulings:

- `resolve`: the branch is fine as it is; requeue it for merge. Allowed only if the
  branch's latest review APPROVED it and no worker has run since (the task escalated
  at merge time). It never skips a gate: the merge engine still runs the full suite
  on the merged tree (invariant 5), and a branch that failed review cannot be
  resolved, so no branch merges without an approving review.
- `rework`: send the task back to its worker and branch with the human's notes as
  feedback; the retry budget restarts (the human replaced the loop that exhausted it).
- `rereview`: the task escalated because its tests could not run in the sandbox
  (an environment fault, e.g. a missing toolchain); once the human has fixed the
  environment, send the unchanged branch back to review. Allowed only when the latest
  machine verdict was that environment failure, so it can never skip a review.
- `cancel`: drop the task (and, by cascade, every task that depends on it).

Every ruling is recorded in the `runs` table (role `human`, no cost), so the history
shows who decided what. Callers hold the state dir's writer lock (`store.lock`).
"""

from __future__ import annotations

from engine.errors import SeamError
from engine.states import Event, State
from engine.task import Task
from engine.usage import RunRecord, Usage
from store.sqlite import TaskStore

HUMAN = "human"  # role and agent id of a human ruling in the runs table
_REVIEW_ROLES = {"reviewer", "gate"}


class EscalationError(SeamError):
    """A ruling that does not apply to this task as it stands."""


def resolve(store: TaskStore, task_id: int, notes: str = "") -> Task:
    """Requeue an escalated task whose approved branch needs no further edits."""
    task = _escalated(store, task_id)
    if not approved_as_is(store, task_id):
        raise EscalationError(
            f"task {task_id}'s branch has no approving review since its last change, so "
            "it cannot be requeued for merge (a branch only merges after an approving "
            "review, invariant 5); use `rework` with notes, or `cancel`")
    done = store.transition(task.id, Event.RESOLVE)
    _record(store, task_id, "resolve", notes)
    return done


def rework(store: TaskStore, task_id: int, notes: str) -> Task:
    """Send an escalated task back to its worker, with the human's guidance."""
    task = _escalated(store, task_id)
    if not notes.strip():
        raise EscalationError("rework needs notes for the worker: without new guidance "
                              "it would repeat the loop that escalated")
    done = store.transition(task.id, Event.REWORK, review_notes=f"From a human: {notes}")
    _record(store, task_id, "rework", notes)
    return done


def rereview(store: TaskStore, task_id: int, notes: str = "") -> Task:
    """Send an escalated task's unchanged branch back to review after an environment
    failure has been fixed."""
    task = _escalated(store, task_id)
    if not env_failed(store, task_id):
        raise EscalationError(
            f"task {task_id} did not escalate because its tests could not run, so there "
            "is nothing to re-review; use `resolve`, `rework` with notes, or `cancel`")
    done = store.transition(task.id, Event.REREVIEW)
    _record(store, task_id, "rereview", notes)
    return done


def cancel(store: TaskStore, task_id: int, notes: str = "") -> list[Task]:
    """Cancel a task and every task that depends on it; return all it cancelled."""
    before = {t.id: t.state for t in store.list()}
    store.transition(task_id, Event.CANCEL)  # IllegalTransition if terminal or merging
    _record(store, task_id, "cancel", notes)
    return [t for t in store.list()
            if t.state is State.CANCELLED and before.get(t.id) is not State.CANCELLED]


def approved_as_is(store: TaskStore, task_id: int) -> bool:
    """True if the latest non-human run on the task is an approving review: the
    branch has not changed since a review approved it."""
    machine = [r for r in store.runs(task_id) if r.role != HUMAN]
    return bool(machine) and machine[-1].role in _REVIEW_ROLES \
        and machine[-1].outcome == "approve"


def env_failed(store: TaskStore, task_id: int) -> bool:
    """True if the latest non-human run on the task is a review that could not run
    the task's tests (an environment failure): the branch has not been judged."""
    machine = [r for r in store.runs(task_id) if r.role != HUMAN]
    return bool(machine) and machine[-1].role in _REVIEW_ROLES \
        and machine[-1].outcome == "env_fail"


def _escalated(store: TaskStore, task_id: int) -> Task:
    task = store.get(task_id)
    if task.state is not State.NEEDS_HUMAN:
        raise EscalationError(f"task {task_id} is {task.state}, not needs_human")
    return task


def _record(store: TaskStore, task_id: int, ruling: str, notes: str) -> None:
    store.record_run(RunRecord(task_id, HUMAN, HUMAN, ruling, 0.0, Usage(), notes))
