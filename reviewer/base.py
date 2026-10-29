"""The pluggable Reviewer interface: the judgment half of the review gate.

A reviewer runtime gets the spec and the diff as git computed it — never the worker's
own account of what it did (invariant 6) — and returns a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from engine.task import Task
from engine.usage import Usage


@dataclass(frozen=True)
class ReviewRequest:
    """What the reviewer judges: the task's spec, its diff, its own tests' output."""

    task: Task
    diff: str
    test_output: str


@dataclass(frozen=True)
class Verdict:
    """`usage` is None when no model ran (the gate's deterministic checks decided)."""

    approve: bool
    notes: str
    cost_usd: float = 0.0
    usage: Usage | None = None


@runtime_checkable
class Reviewer(Protocol):
    """A reviewer runtime. `id` must differ from the authoring worker's id."""

    id: str

    def review(self, request: ReviewRequest) -> Verdict: ...
