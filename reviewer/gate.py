"""The review gate: the first of the two gates before `merged` (doc §6).

Deterministic checks run first, so a reviewer model is only consulted (and paid for)
once the mechanical bar is met:
1. independence — the reviewer is not the author (invariant 6);
2. a non-empty diff that stays inside file_scope (invariant 3);
3. the task's own tests pass on its branch, run in the sandbox (invariants 5, 10);
4. only then the reviewer runtime judges the diff against the spec, given the changed
   files as they were at the merge base (whole by default; with `focused_context`,
   large files are cut to what the diff touches and uses; bounded by
   `context_chars`), so it reviews against the real code, not its memory of it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from engine.errors import SeamError
from engine.git import GitRepo
from engine.scope import path_in_scope
from engine.task import Task
from reviewer.base import Reviewer, ReviewRequest, Verdict
from reviewer.context import split_diff, view
from sandbox.base import Sandbox

_OUTPUT_TAIL = 4000
DEFAULT_CONTEXT_CHARS = 200_000  # original-file context per review, ~50k tokens


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
        return self.reviewer.review(ReviewRequest(
            task=task, diff=diff, test_output=test_output,
            files=files, files_omitted=tuple(omitted)))
