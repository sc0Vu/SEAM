"""Check a plan's claims about existing behavior against the code (doc §12).

A task's contract often states what the code does today ("a missing file is
ignored", "keep < 1 raises ValueError"). If such a claim is wrong, every later gate
enforces it: in the boltons replay (§12, r3) the plan said a missing file raised,
the reviewer rejected the worker's correct code for not raising, and a behavior
change merged. So the planner backs each claim with a probe, a small pytest test
asserting it, and the probes run on the untouched snapshot before any work starts.

On the original code a failing probe is unambiguous evidence: either the claim is
false or the probe is. Either way a human looks before `run` (the plan checkpoint).
Probes are agent-written code: they are parsed on the host, never executed there,
and run only in the sandbox (invariant 10).
"""

from __future__ import annotations

import ast
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from seam_merge.sandbox.base import Sandbox

CLAIMS_DIR = "seam_claims"
_REPORT = ".claims-junit.xml"


@dataclass(frozen=True)
class ClaimResult:
    """One probe's outcome: `holds` (passed), `false` (failed or did not import),
    `invalid` (not Python; not run) or `not run` (the sandbox could not run pytest)."""

    task: str
    name: str
    outcome: str
    detail: str = ""


def _module(task: str, name: str) -> str:
    return "test_" + re.sub(r"\W", "_", f"{task}__{name}")


def check_claims(tasks: list[dict], snapshot: Path, sandbox: Sandbox,
                 timeout: float = 300) -> list[ClaimResult]:
    """Run every task's `claim_probes` on `snapshot` (the original code) in the
    sandbox; one result per probe, in plan order."""
    probes = [(t["key"], p["name"], p["code"]) for t in tasks
              for p in t.get("claim_probes", [])]
    results: dict[str, ClaimResult] = {}
    runnable: dict[str, tuple[str, str]] = {}
    for key, name, code in probes:
        mod = _module(key, name)
        try:
            ast.parse(code)  # parsed, never executed on the host
        except SyntaxError as e:
            results[mod] = ClaimResult(key, name, "invalid", f"not valid Python: {e.msg}")
            continue
        runnable[mod] = (key, name)
        (snapshot / CLAIMS_DIR).mkdir(exist_ok=True)
        (snapshot / CLAIMS_DIR / f"{mod}.py").write_text(code)
    if runnable:
        run = sandbox.run(f"python -m pytest -q -p no:cacheprovider --junitxml={_REPORT} "
                          f"{CLAIMS_DIR}", snapshot, timeout)
        report = snapshot / _REPORT
        failed = _failed_modules(report.read_text()) if report.exists() else None
        for mod, (key, name) in runnable.items():
            if failed is None:
                why = "timed out" if run.timed_out else f"exit {run.exit_code}"
                results[mod] = ClaimResult(key, name, "not run", f"pytest did not run ({why})")
            elif mod in failed:
                results[mod] = ClaimResult(key, name, "false", failed[mod])
            else:
                results[mod] = ClaimResult(key, name, "holds")
    return [results[_module(k, n)] for k, n, _ in probes]


def _failed_modules(junit: str) -> dict[str, str]:
    """`probe module -> first failure message` from pytest's JUnit report. A module
    that failed to import has no per-test cases, only one error entry naming it."""
    failed: dict[str, str] = {}
    for case in ET.fromstring(junit).iter("testcase"):
        where = f"{case.get('classname', '')}.{case.get('name', '')}"
        match = re.search(r"\b(test_\w+__\w+)\b", where.replace(f"{CLAIMS_DIR}.", " "))
        bad = [c for c in case if c.tag in ("failure", "error")]
        if match and bad:
            failed.setdefault(match.group(1), (bad[0].get("message") or "failed")[:300])
    return failed
