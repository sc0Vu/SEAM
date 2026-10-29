"""The orchestrator: runs one team over one goal until no more progress is possible.

Single process, single writer: only this loop's thread touches the task store (the
condition that makes sqlite valid in v1). Worker and review runs execute concurrently
in a thread pool and return results; the loop applies every state change itself.
Merges run in the loop, one at a time (invariant 4).
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from engine.git import GitRepo
from engine.merge import MergeEngine
from engine.states import Event, State
from engine.task import Task
from store.sqlite import TaskStore
from workers.base import Worker, WorkerResult

if TYPE_CHECKING:
    from coordinator.lead import Lead
    from reviewer.base import Verdict


class Gate(Protocol):
    def review(self, task: Task) -> Verdict: ...


@dataclass(frozen=True)
class RunReport:
    """Where every task ended up, and what the run cost."""

    tasks: list[Task]
    spent_usd: float
    budget_exhausted: bool

    def ids_in(self, state: State) -> list[int]:
        return [t.id for t in self.tasks if t.state is state]


@dataclass(frozen=True)
class _Job:
    kind: str  # "work" | "review"
    task_id: int
    worker: Worker | None = None


class Orchestrator:
    """Dispatches tasks to workers, reviews submissions, and merges approvals.

    Upholds: one worktree + branch per task, created from the target (invariant 1);
    claims go through the store's disjoint claim (2); scope requests go to the lead
    (3); rework re-runs on the task's own worker and branch; no new agent run starts
    once the lead's budget is spent.
    """

    def __init__(
        self,
        store: TaskStore,
        lead: Lead,
        repo: GitRepo,
        workers: list[Worker],
        gate: Gate,
        merger: MergeEngine,
        worktree_root: Path,
        target: str = "main",
    ) -> None:
        if len({w.id for w in workers}) != len(workers):
            raise ValueError("worker ids must be unique")
        self.store = store
        self.lead = lead
        self.repo = repo
        self.workers = workers
        self.gate = gate
        self.merger = merger
        self.worktree_root = Path(worktree_root)
        self.target = target
        # Denied scope requests, retried as merges free scope. In memory only: after a
        # restart a blocked task waits for the lead (or a human) to unblock it.
        self._pending_scope: dict[int, list[str]] = {}

    def run(self) -> RunReport:
        self.merger.recover()
        jobs: dict[Future, _Job] = {}
        with ThreadPoolExecutor(max_workers=len(self.workers) + 1) as pool:
            while True:
                self._drain_merges()
                self._retry_blocked()
                self._dispatch(pool, jobs)
                self._start_review(pool, jobs)
                if not jobs:
                    break  # nothing running and nothing could start: done or stuck
                done, _ = wait(jobs, return_when=FIRST_COMPLETED)
                for fut in done:
                    self._finish(fut, jobs.pop(fut))
        return RunReport(
            tasks=self.store.list(),
            spent_usd=self.lead.budget.spent_usd,
            budget_exhausted=self.lead.budget.exhausted,
        )

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, pool: ThreadPoolExecutor, jobs: dict[Future, _Job]) -> None:
        busy_workers = {j.worker.id for j in jobs.values() if j.worker}
        busy_tasks = {j.task_id for j in jobs.values()}
        for w in self.workers:
            if w.id in busy_workers or not self.lead.can_dispatch():
                continue
            task = self._rework_for(w, busy_tasks) or self._claim_for(w)
            if task is None:
                continue
            busy_tasks.add(task.id)
            fut = pool.submit(w.run, task, Path(task.worktree_path or ""))
            jobs[fut] = _Job("work", task.id, w)

    def _rework_for(self, w: Worker, busy: set[int]) -> Task | None:
        """An in_progress task of this worker's that needs another run (after
        changes_requested, a failed run, or a granted scope request)."""
        for t in self.store.list(State.IN_PROGRESS):
            if t.owner == w.id and t.id not in busy:
                return t
        return None

    def _claim_for(self, w: Worker) -> Task | None:
        task = self.store.claim(w.id, w.type)
        if task is None:
            return None
        branch = f"seam/task-{task.id}"
        path = self.worktree_root / f"task-{task.id}"
        if not path.exists():
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            self.repo.add_worktree(path, branch, self.target)
        return self.store.transition(
            task.id, Event.START, branch=branch, worktree_path=str(path)
        )

    def _start_review(self, pool: ThreadPoolExecutor, jobs: dict[Future, _Job]) -> None:
        if any(j.kind == "review" for j in jobs.values()) or not self.lead.can_dispatch():
            return
        submitted = self.store.list(State.SUBMITTED)
        if not submitted:
            return
        task = self.store.transition(submitted[0].id, Event.PICKUP)
        jobs[pool.submit(self.gate.review, task)] = _Job("review", task.id)

    # -- results -------------------------------------------------------------

    def _finish(self, fut: Future, job: _Job) -> None:
        if job.kind == "work":
            self._finish_work(job.task_id, fut)
        else:
            self._finish_review(job.task_id, fut)

    def _finish_work(self, task_id: int, fut: Future) -> None:
        try:
            result: WorkerResult = fut.result()
        except Exception as e:  # a crashed run is a bounded attempt, not a crash of SEAM
            self.lead.budget.charge(getattr(e, "cost_usd", 0.0))
            self.store.transition(task_id, Event.FAIL, review_notes=f"worker run failed: {e}")
            return
        self.lead.budget.charge(result.cost_usd)
        if result.scope_request:
            # Notes go to the next worker run as rejection feedback, so they are set
            # only when this request exhausts the attempts and a human must rule on it.
            last = self.store.get(task_id).attempts + 1 >= self.store.max_attempts
            notes = {"review_notes": "out of attempts while requesting scope expansion: "
                     f"{list(result.scope_request)}"} if last else {}
            asked = self.store.transition(task_id, Event.REQUEST_SCOPE, **notes)
            if asked.state is State.NEEDS_HUMAN:
                return
            ruled = self.lead.decide_scope(task_id, list(result.scope_request))
            if ruled.state is State.BLOCKED:
                self._pending_scope[task_id] = list(result.scope_request)
            return
        task = self.store.get(task_id)
        wt = GitRepo(task.worktree_path or "")
        if not wt.stage_all():
            self.store.transition(task_id, Event.FAIL, review_notes="worker run changed nothing")
            return
        wt.commit_staged(f"task {task_id}: {task.spec.description}")
        diff = self.repo.diff(self.target, task.branch or "")
        self.store.transition(task_id, Event.SUBMIT, diff=diff)

    def _finish_review(self, task_id: int, fut: Future) -> None:
        try:
            verdict: Verdict = fut.result()
        except Exception as e:
            self.store.transition(task_id, Event.FAIL, review_notes=f"review run failed: {e}")
            return
        self.lead.budget.charge(verdict.cost_usd)
        if verdict.approve:
            self.store.transition(task_id, Event.APPROVE, review_notes=verdict.notes)
            self.store.transition(task_id, Event.ENQUEUE)
        else:
            self.store.transition(task_id, Event.REQUEST_CHANGES, review_notes=verdict.notes)

    def _retry_blocked(self) -> None:
        for task_id, globs in list(self._pending_scope.items()):
            if self.store.get(task_id).state is not State.BLOCKED:
                del self._pending_scope[task_id]
            elif self.lead.decide_scope(task_id, globs).state is State.IN_PROGRESS:
                del self._pending_scope[task_id]

    # -- merge ---------------------------------------------------------------

    def _drain_merges(self) -> None:
        """Merge every queued task, one at a time. A task requeued because main moved
        mid-merge gets one immediate retry; after that it waits for the next pass."""
        retried: set[int] = set()
        while True:
            task = self.merger.merge_next()
            if task is None:
                return
            if task.state is State.MERGED and task.worktree_path:
                self.repo.remove_worktree(Path(task.worktree_path))
            if task.state is State.QUEUED_FOR_MERGE:
                if task.id in retried:
                    return
                retried.add(task.id)
