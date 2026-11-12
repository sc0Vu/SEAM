"""Mechanical checks on a plan's test commands against the full suite (doc §12).

A task passes review on its own `test_cmd` (gate 1) and merges only if the FULL
suite passes on the merged tree (gate 2, invariant 5). If the suite collects a kind
of test the task's command does not, such as doctests, a task can pass its own tests,
pass review, and only then fail the merge: in bench-3 (doc §12) every planned
`test_cmd` left out the `--doctest-modules` the merge suite ran. These checks warn at
the plan checkpoint; they never block.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class SuiteGap:
    """A task whose test command will not catch what the merge suite will."""

    task: str
    message: str


def _pytest_flags(cmd: str) -> set[str] | None:
    """The flags of a pytest command that change WHAT is collected (doctest
    options), or None if the command is not a pytest run."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    if not any(t == "pytest" or t.endswith("/pytest") for t in tokens):
        return None
    return {t.split("=", 1)[0] for t in tokens if t.startswith("--doctest")}


def check_test_cmds(tasks: list[dict], suite_cmd: str) -> list[SuiteGap]:
    """Tasks with no test command, and pytest tasks missing a collection flag the
    pytest suite uses. Non-pytest suites and non-pytest task commands (a config check,
    another language) are not compared: there is no common notion of a flag."""
    suite = _pytest_flags(suite_cmd)
    gaps = []
    for t in tasks:
        cmd = t.get("test_cmd", "")
        if not cmd.strip():
            gaps.append(SuiteGap(t["key"], "no test_cmd: review will run none of its tests"))
            continue
        mine = _pytest_flags(cmd)
        if suite is None or mine is None:
            continue
        missing = sorted(suite - mine)
        if missing:
            gaps.append(SuiteGap(t["key"], (
                f"test_cmd lacks {', '.join(missing)}, which the full suite uses: the task "
                f"can pass its own tests and review, then fail at merge")))
    return gaps
