"""The serialized merge engine: the second gate (doc/architecture.md §6).

For the one task in the `merging` slot: trial-merge its branch onto the current target
off to the side, run the FULL suite on the merged tree in the sandbox, and only if it
is green, publish by moving the target with a compare-and-swap. Any textual conflict
or red suite escalates to `needs_human`; nothing is ever auto-resolved (invariant 9).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from engine.errors import TargetCheckedOut
from engine.git import GitRepo
from engine.states import Event, State
from engine.task import Task
from sandbox.base import Sandbox
from store.sqlite import TaskStore

_OUTPUT_TAIL = 4000


@dataclass(frozen=True)
class _Ready:
    base: str
    merge_sha: str


@dataclass(frozen=True)
class _Escalate:
    notes: str


class MergeEngine:
    """Merges approved tasks into `target`, one at a time.

    Upholds:
    - invariant 4: a task enters merging only via the store's single slot;
    - invariant 5: `merged` only after review (the state machine) AND a green full
      suite on the merged tree, run here, distinct from the reviewer's branch run;
    - invariant 9: textual or semantic conflict -> `needs_human`, never auto-resolved;
    - invariant 10: the suite runs in the sandbox on an exported copy of the tree;
    - invariant 12: the target moves only after both gates, as one CAS, with the merge
      commit recorded first, so `recover` can always settle an interrupted merge.
    """

    def __init__(
        self,
        store: TaskStore,
        repo: GitRepo,
        sandbox: Sandbox,
        suite_cmd: str,
        target: str = "main",
        suite_timeout: float = 1800,
        scratch: Path | None = None,
    ) -> None:
        self.store = store
        self.repo = repo
        self.sandbox = sandbox
        self.suite_cmd = suite_cmd
        self.target = target
        self.suite_timeout = suite_timeout
        self.scratch = scratch

    def recover(self) -> list[Task]:
        """Settle tasks left in `merging` by a crash: `merged` if their merge commit
        landed on the target, else requeued. Call once at startup, before merging."""
        return self.store.recover_merging(
            landed=lambda sha: self.repo.is_ancestor(sha, self.target)
        )

    def merge_next(self) -> Task | None:
        """Merge the next queued task (priority desc, id asc). Returns it in its
        resulting state, or None if nothing is queued.

        If preparing the merge fails unexpectedly, the task is requeued (the target was
        never touched) and the error re-raised.
        """
        if self.repo.checked_out_in(self.target):
            raise TargetCheckedOut(self.target)
        queued = self.store.list(State.QUEUED_FOR_MERGE)
        if not queued:
            return None
        nxt = min(queued, key=lambda t: (-t.spec.priority, t.id))
        task = self.store.transition(nxt.id, Event.TAKE)

        try:
            outcome = self._prepare(task)
        except BaseException:
            self.store.transition(task.id, Event.RECOVER)
            raise
        if isinstance(outcome, _Escalate):
            return self.store.transition(task.id, Event.MERGE_CONFLICT, review_notes=outcome.notes)

        # Both gates passed. Record, then publish, then mark: a crash between any two
        # of these steps is settled by `recover` (doc §6 crash-safety).
        self.store.record_merge_commit(task.id, outcome.base, outcome.merge_sha)
        if not self.repo.update_branch(self.target, outcome.merge_sha, expected=outcome.base):
            # The target moved underneath us (outside SEAM). Nothing was published;
            # requeue so the next attempt merges against the new target.
            return self.store.transition(task.id, Event.RECOVER)
        return self.store.transition(task.id, Event.MERGE_CLEAN)

    def _prepare(self, task: Task) -> _Ready | _Escalate:
        base = self.repo.rev_parse(self.target)
        branch = self.repo.rev_parse(task.branch or "")
        trial = self.repo.merge_tree(base, branch)
        if trial.conflicts:
            return _Escalate(
                "textual conflict with "
                f"{self.target} in: {', '.join(trial.conflicts)}. Scopes are disjoint, so "
                f"{self.target} changed outside SEAM; a human must reconcile."
            )
        merge_sha = self.repo.commit_tree(
            trial.tree,
            [base, branch],
            f"Merge task {task.id}: {task.spec.description}\n\nSEAM-Task: {task.id}\n"
            f"SEAM-Worker: {task.owner}",
        )
        with tempfile.TemporaryDirectory(prefix="seam-merge-", dir=self.scratch) as d:
            self.repo.export(merge_sha, Path(d))
            result = self.sandbox.run(self.suite_cmd, Path(d), self.suite_timeout)
        if not result.ok:
            why = "timed out" if result.timed_out else f"failed (exit {result.exit_code})"
            return _Escalate(
                f"full suite {why} on the merged tree with no textual conflict: "
                f"likely a semantic conflict with {self.target}.\n"
                f"--- suite output (tail) ---\n{result.output[-_OUTPUT_TAIL:]}"
            )
        return _Ready(base=base, merge_sha=merge_sha)
