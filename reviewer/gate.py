"""The review gate: the first of the two gates before `merged` (doc §6).

Deterministic checks run first, so a reviewer model is only consulted (and paid for)
once the mechanical bar is met:
1. independence — the reviewer is not the author (invariant 6);
2. a non-empty diff that stays inside file_scope (invariant 3);
3. the task's own tests pass on its branch, run in the sandbox (invariants 5, 10);
4. only then the reviewer runtime judges the diff against the spec, given the changed
   files as they were at the merge base (whole by default; with `focused_context`,
   large files are cut to what the diff touches and uses; bounded by
   `context_chars`), so it reviews against the real code, not its memory of it;
5. with `probe_cmd` set, the reviewer's adversarial probe tests run in the sandbox on
   the branch, and any genuine failure rejects the change (doc §11).
"""

from __future__ import annotations

import ast
import tempfile
from dataclasses import replace
from pathlib import Path

from engine.errors import SeamError
from engine.git import GitRepo
from engine.scope import path_in_scope
from engine.task import Task
from reviewer.base import Probe, Reviewer, ReviewRequest, Verdict
from reviewer.context import split_diff, view
from sandbox.base import Sandbox

_OUTPUT_TAIL = 4000
DEFAULT_CONTEXT_CHARS = 200_000  # original-file context per review, ~50k tokens
PROBE_DIR = "seam_probes"
DEFAULT_PROBE_CMD = f"python -m pytest -q -p no:cacheprovider {PROBE_DIR}"
# pytest exit codes that mean the code under test misbehaved: 1 = a test failed,
# 2 = collection failed (e.g. the probe imports what the change never added). Others
# (no tests collected, usage error, timeout) mean the probes are broken, not the code.
_PROBE_FAILED = {1, 2}


class ReviewerNotIndependent(SeamError):
    """The reviewer's id matches the task's authoring worker (invariant 6)."""


class ReviewGate:
    """Reviews one submitted task; returns a verdict, never mutates task state."""

    def __init__(
        self,
        repo: GitRepo,
        sandbox: Sandbox,
        reviewer: Reviewer,
        target: str = "main",
        test_timeout: float = 900,
        scratch: Path | None = None,
        context_chars: int = DEFAULT_CONTEXT_CHARS,
        focused_context: bool = False,
        probe_cmd: str | None = None,
        probe_timeout: float = 300,
    ) -> None:
        """`focused_context`: ~3x cheaper reviews of large files, but on boltons-bench-2
        it caught a confirmed bug 0/3 times vs 1/2 with whole files (doc §8), so it is
        opt-in until measured on more cases."""
        self.repo = repo
        self.sandbox = sandbox
        self.reviewer = reviewer
        self.target = target
        self.test_timeout = test_timeout
        self.scratch = scratch
        self.context_chars = context_chars
        self.focused_context = focused_context
        self.probe_cmd = probe_cmd
        self.probe_timeout = probe_timeout

    def originals(self, branch: str, changed: list[str],
                  diff: str = "") -> tuple[dict[str, str], list[str]]:
        """The changed files' text at the merge base, as `reviewer.context.view` shows
        it (whole if small, else focused on what `diff` touches and uses), smallest
        first within the budget; returns (files, omitted). New files have no
        original: the diff is all of them."""
        base = self.repo.merge_base(self.target, branch)
        changes = split_diff(diff)
        views = []
        for path in changed:
            blob = self.repo.read_file(base, path)
            if blob is None:
                continue
            if b"\0" in blob:
                views.append((path, None))
                continue
            text = blob.decode("utf-8", "replace")
            views.append((path, view(text, changes.get(path), self.focused_context)))
        files: dict[str, str] = {}
        omitted: list[str] = []
        used = 0
        for path, text in sorted(views, key=lambda v: len(v[1] or "")):
            if text is None or used + len(text) > self.context_chars:
                omitted.append(path)
                continue
            files[path] = text
            used += len(text)
        return files, sorted(omitted)

    def review(self, task: Task) -> Verdict:
        if self.reviewer.id == task.owner:
            raise ReviewerNotIndependent(f"reviewer {self.reviewer.id} authored task {task.id}")
        branch = task.branch or ""
        changed = self.repo.changed_files(self.target, branch)
        if not changed:
            return Verdict(False, "empty diff: the branch changes nothing.")
        outside = [p for p in changed if not path_in_scope(p, task.spec.file_scope)]
        if outside:
            return Verdict(
                False,
                f"edited outside file_scope {list(task.spec.file_scope)}: {outside}. "
                "Revert those files; if they are truly needed, request a scope expansion.",
            )

        test_output = ""
        if task.spec.test_cmd:
            with tempfile.TemporaryDirectory(prefix="seam-review-", dir=self.scratch) as d:
                self.repo.export(self.repo.rev_parse(branch), Path(d))
                result = self.sandbox.run(task.spec.test_cmd, Path(d), self.test_timeout)
            test_output = result.output[-_OUTPUT_TAIL:]
            if not result.ok:
                why = "timed out" if result.timed_out else f"failed (exit {result.exit_code})"
                return Verdict(False, f"task tests {why} on the branch:\n{test_output}")

        diff = self.repo.diff(self.target, branch)
        files, omitted = self.originals(branch, changed, diff)
        verdict = self.reviewer.review(ReviewRequest(
            task=task, diff=diff, test_output=test_output,
            files=files, files_omitted=tuple(omitted)))
        if self.probe_cmd and verdict.probes:
            return self.run_probes(branch, verdict)
        return verdict

    def run_probes(self, branch: str, verdict: Verdict) -> Verdict:
        """Run the reviewer's probes on the branch in the sandbox; a genuine failure
        rejects (always: the failing test is the evidence). Probes that are not valid
        Python are dropped unrun, so a reviewer typo cannot reject correct work."""
        valid: list[Probe] = []
        for p in verdict.probes:
            try:
                ast.parse(p.code)  # parsed, never executed on the host
                valid.append(p)
            except SyntaxError:
                pass
        dropped = len(verdict.probes) - len(valid)
        if not valid:
            return replace(verdict,
                           notes=verdict.notes + f"\n[probes: {dropped} invalid, none run]")
        with tempfile.TemporaryDirectory(prefix="seam-probe-", dir=self.scratch) as d:
            root = Path(d)
            self.repo.export(self.repo.rev_parse(branch), root)
            (root / PROBE_DIR).mkdir(exist_ok=True)
            for p in valid:
                (root / PROBE_DIR / p.name).write_text(p.code)
            result = self.sandbox.run(self.probe_cmd, root, self.probe_timeout)
        out = result.output[-_OUTPUT_TAIL:]
        names = ", ".join(p.name for p in valid)
        if not result.timed_out and result.exit_code in _PROBE_FAILED:
            code = "\n\n".join(f"# {p.name}\n{p.code}" for p in valid)
            return replace(verdict, approve=False, notes=(
                "A reviewer probe test FAILS on your branch, so the change is rejected. "
                "Make the code satisfy it (it asserts only what the task and spec "
                f"require).\n\n--- probes ---\n{code}\n\n--- pytest output ---\n{out}"
                f"\n\n--- reviewer notes ---\n{verdict.notes}"))
        why = "timed out" if result.timed_out else ("passed" if result.ok
                                                    else f"broken (exit {result.exit_code})")
        extra = f", {dropped} invalid dropped" if dropped else ""
        return replace(verdict, notes=verdict.notes + f"\n[probes {why}: {names}{extra}]")
