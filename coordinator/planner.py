"""The pluggable Planner interface: how the lead turns a goal into a plan (doc §12).

A planner reads a snapshot of the repository and a goal in prose and drafts a plan in
the same format a hand-written plan uses; `coordinator.lead.plan_from_dict` validates
it with the same rules. The lead stays the sole task producer (invariant 7): a draft
becomes tasks only through `Lead.submit_plan`. Runtime adapters live in `planners/`;
nothing runtime-specific is imported here (invariant 8).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from coordinator.lead import Plan
from engine.errors import SeamError
from engine.usage import Usage


@dataclass(frozen=True)
class PlanDraft:
    """A validated plan plus what producing it cost and the planner's own notes
    (rationale, scopes it knows will overlap, risks it saw)."""

    plan: Plan
    notes: str = ""
    cost_usd: float = 0.0
    usage: Usage | None = None


class PlannerFailed(SeamError):
    """The planner produced no valid plan. Carries what the run still cost."""

    def __init__(self, message: str, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


@runtime_checkable
class Planner(Protocol):
    """A planner runtime. `repo_snapshot` is a plain export of the target (no `.git`);
    implementations must only read it, never edit it."""

    id: str

    def plan(self, goal: str, repo_snapshot: Path) -> PlanDraft: ...
