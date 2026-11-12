"""The task record (doc/architecture.md §4). Plain data, no behavior."""

from __future__ import annotations

from dataclasses import dataclass

from seam_merge.engine.states import State


@dataclass(frozen=True)
class TaskSpec:
    """What the lead declares when it creates a task. The lead is the sole producer
    (invariant 7); everything else about a task is runtime state the engine owns."""

    goal_id: str
    type: str
    description: str
    file_scope: tuple[str, ...]
    spec_ref: str = ""
    blocked_by: tuple[int, ...] = ()
    priority: int = 0
    test_cmd: str = ""
    # Pre-existing tests (`path::[Class::]name`) this task may rewrite because it
    # deliberately changes the behavior they assert. Any other pre-existing test must
    # keep its original statements; the review gate rejects the change otherwise.
    may_change_tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class Task:
    """A task as stored: its spec plus the state the engine tracks."""

    id: int
    spec: TaskSpec
    state: State
    attempts: int = 0
    owner: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    review_notes: str = ""
    diff: str = ""
    base_sha: str | None = None
    merge_sha: str | None = None
    pending_scope: tuple[str, ...] = ()  # a denied scope request, retried while blocked
    # Separate work the worker found and set aside instead of doing it here, from its
    # latest submission. Proposals, not tasks: only the lead creates tasks (inv. 7).
    proposals: tuple[str, ...] = ()
