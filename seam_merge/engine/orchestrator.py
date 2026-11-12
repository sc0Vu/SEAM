"""The orchestrator: runs one team over one goal until no more progress is possible.

Single process, single writer: only this loop's thread touches the task store (the
condition that makes sqlite valid in v1). Worker and review runs execute concurrently
in a thread pool and return results; the loop applies every state change itself.
Merges run in the loop, one at a time (invariant 4).
"""

from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from seam_merge.engine.git import GitRepo
from seam_merge.engine.merge import MergeEngine
from seam_merge.engine.states import Event, State
from seam_merge.engine.task import Task
from seam_merge.engine.usage import RunRecord, Usage
from seam_merge.store.sqlite import TaskStore
from seam_merge.workers.base import Worker, WorkerResult

if TYPE_CHECKING:
    from seam_merge.coordinator.lead import Lead
    from seam_merge.reviewer.base import Verdict


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
            task = (self._rework_for(w, busy_tasks) or self._stale_claim_for(w, busy_tasks)
                    or self._claim_for(w))
            if task is None:
                continue
            busy_tasks.add(task.id)
            fut = pool.submit(w.run, task, Path(task.worktree_path or ""))
            jobs[fut] = _Job("work", task.id, w)

    def _rework_for(self, w: Worker, busy: set[int]) -> Task | None:
        """An in_progress task that needs another run (after changes_requested, a
        failed run, a granted scope request, or a human's `rework`): this worker's
        own first, else one of its type whose owner is not in this run, which it
        adopts (otherwise that task would wait forever for a worker that isn't here)."""
        mine = {x.id for x in self.workers}
        orphan = None
        for t in self.store.list(State.IN_PROGRESS):
            if t.id in busy:
                continue
            if t.owner == w.id:
                return t
            if orphan is None and t.owner not in mine and t.spec.type == w.type:
                orphan = t
        return self.store.reassign(orphan.id, w.id) if orphan else None

    def _stale_claim_for(self, w: Worker, busy: set[int]) -> Task | None:
        """A task of this worker's type left `claimed`: this loop claims and starts a
        task in one step, so one found here was claimed by a run that stopped before
        starting it (a crash, or a worktree that could not be created). It keeps its
        claim, and its scope, and is started now."""
        for t in self.store.list(State.CLAIMED):
            if t.id not in busy and t.spec.type == w.type:
                return self._start(self.store.reassign(t.id, w.id) if t.owner != w.id else t)
        return None

    def _claim_for(self, w: Worker) -> Task | None:
        task = self.store.claim(w.id, w.type)
        return self._start(task) if task else None

    def _start(self, task: Task) -> Task:
        """Give a claimed task its worktree and branch, and move it to in_progress."""
        branch = f"seam/{self.store.branch_namespace}/task-{task.id}"
        path = self.worktree_root / f"task-{task.id}"
        if path.exists():  # left by an earlier run: record the branch it is really on
            branch = GitRepo(path).current_branch() or branch
        else:
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
            self._finish_work(job.task_id, fut, job.worker.id if job.worker else "?")
        else:
            self._finish_review(job.task_id, fut)

    def _record(self, task_id: int, role: str, agent: str, outcome: str,
                cost: float, usage: Usage | None, notes: str = "", probes: str = "") -> None:
        self.store.record_run(
            RunRecord(task_id, role, agent, outcome, cost, usage or Usage(), notes, probes))

    def _finish_work(self, task_id: int, fut: Future, worker_id: str) -> None:
        try:
            result: WorkerResult = fut.result()
        except Exception as e:  # a crashed run is a bounded attempt, not a crash of SEAM
            cost = getattr(e, "cost_usd", 0.0)
            self.lead.budget.charge(cost)
            self._record(task_id, "worker", worker_id, "failed", cost, getattr(e, "usage", None))
            self.store.transition(task_id, Event.FAIL, review_notes=f"worker run failed: {e}")
            return
        self.lead.budget.charge(result.cost_usd)
        self._record(task_id, "worker", worker_id,
                     "scope_request" if result.scope_request else "diff",
                     result.cost_usd, result.usage)
        if result.scope_request:
            # Notes go to the next worker run as rejection feedback, so they are set
            # only when this request exhausts the attempts and a human must rule on it.
            last = self.store.get(task_id).attempts + 1 >= self.store.max_attempts
            notes = {"review_notes": "out of attempts while requesting scope expansion: "
                     f"{list(result.scope_request)}"} if last else {}
            asked = self.store.transition(task_id, Event.REQUEST_SCOPE, **notes)
            if asked.state is State.NEEDS_HUMAN:
                return
            # A denial is stored with the task and retried by `_retry_blocked`.
            self.lead.decide_scope(task_id, list(result.scope_request))
            return
        task = self.store.get(task_id)
        wt = GitRepo(task.worktree_path or "", author=self.repo.author)
        if not wt.stage_all():
            self.store.transition(task_id, Event.FAIL, review_notes="worker run changed nothing")
            return
        # Author: the operator; committer: SEAM; the models that wrote it and the
        # SEAM worker that ran them are in the trailers (doc §4, commit identity).
        credits = "".join(f"\nCo-Authored-By: {who}" for who in result.co_authors)
        wt.commit_staged(
            f"task {task_id}: {task.spec.description}\n\n"
            f"SEAM-Task: {task_id}\nSEAM-Worker: {worker_id}\nSEAM-Attempt: {task.attempts + 1}"
            + credits)
        diff = self.repo.diff(self.target, task.branch or "")
        self.store.transition(task_id, Event.SUBMIT, diff=diff,
                              proposals=json.dumps(list(result.proposals)))

    def _finish_review(self, task_id: int, fut: Future) -> None:
        try:
            verdict: Verdict = fut.result()
        except Exception as e:
            self._record(task_id, "reviewer", self._reviewer_id(), "failed", 0.0, None)
            self.store.transition(task_id, Event.FAIL, review_notes=f"review run failed: {e}")
            return
        self.lead.budget.charge(verdict.cost_usd)
        by_model = verdict.usage is not None or verdict.cost_usd > 0
        outcome = "approve" if verdict.approve else ("env_fail" if verdict.environment
                                                     else "reject")
        self._record(task_id, "reviewer" if by_model else "gate",
                     self._reviewer_id() if by_model else "gate",
                     outcome, verdict.cost_usd, verdict.usage,
                     verdict.notes,
                     json.dumps([{"name": p.name, "code": p.code} for p in verdict.probes])
                     if verdict.probes else "")
        if verdict.approve:
            self.store.transition(task_id, Event.APPROVE, review_notes=verdict.notes)
            self.store.transition(task_id, Event.ENQUEUE)
        elif verdict.environment:  # the sandbox's fault: no worker can fix it
            self.store.transition(task_id, Event.ESCALATE, review_notes=verdict.notes)
        else:
            self.store.transition(task_id, Event.REQUEST_CHANGES, review_notes=verdict.notes)

    def _reviewer_id(self) -> str:
        return getattr(getattr(self.gate, "reviewer", None), "id", "reviewer")

    def _retry_blocked(self) -> None:
        """Re-ask the lead for every denied scope request (stored with the task, so
        this includes requests denied before a restart); merges may have freed it."""
        for t in self.store.list(State.BLOCKED):
            if t.pending_scope:
                self.lead.decide_scope(t.id, list(t.pending_scope))

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
                # Its commits are in the target now (and the trailers name the task),
                # so the branch is only clutter. Escalated tasks keep theirs.
                self.repo.delete_merged_branch(task.branch or "", self.target)
            if task.state is State.QUEUED_FOR_MERGE:
                if task.id in retried:
                    return
                retried.add(task.id)
