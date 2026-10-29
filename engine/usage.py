"""Token usage per agent run: what the budget is spent on, made inspectable.

Plain data (no runtime imports): adapters fill `Usage` from whatever their runtime
reports; the orchestrator records one `RunRecord` per worker run and per review.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Usage:
    """Tokens one agent run consumed. `input_tokens` is uncached input only; cache
    reads are billed far cheaper than cache writes, so the three are kept apart."""

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(*(getattr(self, f.name) + getattr(other, f.name) for f in fields(self)))

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Share of input served from the prompt cache (0.0 with no input)."""
        return self.cache_read_tokens / self.total_input if self.total_input else 0.0


@dataclass(frozen=True)
class RunRecord:
    """One agent run against a task. `role` is `worker`, `reviewer`, or `gate` (a
    review rejected by the deterministic checks, so no model ran)."""

    task_id: int
    role: str
    agent_id: str
    outcome: str
    cost_usd: float = 0.0
    usage: Usage = Usage()
    notes: str = ""  # a review's notes (kept even after a later run overwrites the task's)


def totals_by_role(runs: list[RunRecord]) -> dict[str, tuple[int, float, Usage]]:
    """`role -> (runs, cost_usd, usage)` summed over `runs`."""
    out: dict[str, tuple[int, float, Usage]] = {}
    for r in runs:
        n, cost, usage = out.get(r.role, (0, 0.0, Usage()))
        out[r.role] = (n + 1, cost + r.cost_usd, usage + r.usage)
    return out


def format_usage(u: Usage) -> str:
    return (f"in {u.input_tokens:,} + cache read {u.cache_read_tokens:,} + cache write "
            f"{u.cache_write_tokens:,} (hit {u.cache_hit_rate:.0%}), out {u.output_tokens:,}")
