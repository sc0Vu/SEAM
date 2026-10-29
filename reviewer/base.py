"""The pluggable Reviewer interface: the judgment half of the review gate.

A reviewer runtime gets the spec and the diff as git computed it — never the worker's
own account of what it did (invariant 6) — and returns a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from engine.task import Task
from engine.usage import Usage


@dataclass(frozen=True)
class ReviewRequest:
    """What the reviewer judges: the task's spec, its diff, its own tests' output, and
    the changed files as they were before the change (`files`, path -> text), so it
    judges the diff against the real surrounding code rather than guessing.
    `files_omitted` lists changed files left out (too large, or binary)."""

    task: Task
    diff: str
    test_output: str
    files: dict[str, str] = field(default_factory=dict)
    files_omitted: tuple[str, ...] = ()


@dataclass(frozen=True)
class Probe:
    """An adversarial test the reviewer wrote against the change (doc §11). It must pass
    on any correct implementation; the gate runs it in the sandbox, never merges it."""

    name: str  # a pytest file name: test_*.py, sanitized by the reviewer adapter
    code: str


@dataclass(frozen=True)
class Verdict:
    """`usage` is None when no model ran (the gate's deterministic checks decided).
    `probes` are the reviewer's adversarial tests; the gate decides what they prove."""

    approve: bool
    notes: str
    cost_usd: float = 0.0
    usage: Usage | None = None
    probes: tuple[Probe, ...] = ()


@runtime_checkable
class Reviewer(Protocol):
    """A reviewer runtime. `id` must differ from the authoring worker's id."""

    id: str

    def review(self, request: ReviewRequest) -> Verdict: ...
