"""The sandbox interface (invariant 10).

Agent-written code, build steps, and every test run go through `Sandbox.run` — never
straight onto the host. The engine depends only on this interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RunResult:
    """Outcome of one sandboxed command. `timed_out` implies `ok` is False."""

    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class Sandbox(Protocol):
    """Runs `cmd` with `workdir` as its working directory, isolated from the host."""

    def run(self, cmd: str, workdir: Path, timeout: float) -> RunResult: ...
