"""The pluggable Worker interface (invariant 8).

A worker is any headless coding agent behind `run(task, worktree) -> WorkerResult`.
Runtime adapters (Claude Code, AgentScope) implement it in their own modules; nothing
runtime-specific is imported by the engine, coordinator, or store.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from engine.task import Task


@dataclass(frozen=True)
class WorkerResult:
    """What a worker hands back. Exactly one of the two outcomes:

    - `diff`: the finished change, as a unified diff against the task's branch base.
      It is untrusted data until review and merge (invariant 10).
    - `scope_request`: globs outside the task's file_scope the worker needs. The
      worker STOPS instead of editing them (invariant 3); the lead decides.
    """

    diff: str = ""
    scope_request: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if bool(self.diff) == bool(self.scope_request):
            raise ValueError("WorkerResult needs exactly one of diff or scope_request")


@runtime_checkable
class Worker(Protocol):
    """A worker runtime. Implementations must edit only inside `worktree` and only
    within `task.spec.file_scope`, and must not touch task state themselves."""

    type: str

    def run(self, task: Task, worktree: Path) -> WorkerResult: ...
