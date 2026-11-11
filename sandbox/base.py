"""The sandbox interface (invariant 10).

Agent-written code, build steps, and every test run go through `Sandbox.run` — never
straight onto the host. The engine depends only on this interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

ENV_FAILURE_CODES = frozenset({125, 126, 127})


@dataclass(frozen=True)
class RunResult:
    """Outcome of one sandboxed command. `timed_out` implies `ok` is False."""

    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def env_failure(self) -> bool:
        """The command could not run at all, so nothing was tested: the shell's 127
        (command not found) or 126 (not executable), or docker's own 125 (the
        container failed to start). A fault of the sandbox image or setup, never of
        the code under test, so no agent can fix it."""
        return not self.timed_out and self.exit_code in ENV_FAILURE_CODES


@runtime_checkable
class Sandbox(Protocol):
    """Runs `cmd` with `workdir` as its working directory, isolated from the host."""

    def run(self, cmd: str, workdir: Path, timeout: float) -> RunResult: ...
