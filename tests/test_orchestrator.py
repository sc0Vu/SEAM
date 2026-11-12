"""End-to-end orchestrator runs on real throwaway git repos.

Workers are scripted (they write files into their worktree), the reviewer is scripted,
and the sandbox is the inspect-only fake, so these exercise the engine's integration
logic without any model calls. They are the engine-level regression tests for the two
v1 cases; the real-runtime run is examples/greeter (see its README).
"""

import json
import tempfile
import threading
from pathlib import Path

import pytest

from seam_merge.coordinator.lead import Budget, Lead
from seam_merge.engine.git import GitRepo, Identity
from seam_merge.engine.merge import MergeEngine
from seam_merge.engine.orchestrator import Orchestrator
from seam_merge.engine.states import Event, State
from seam_merge.engine.task import TaskSpec
from seam_merge.reviewer.base import ReviewRequest, Verdict
from seam_merge.reviewer.gate import ReviewGate
from seam_merge.store import TaskStore
from seam_merge.workers.base import WorkerFailed, WorkerResult
from tests.fakes import ImportCheckSandbox
from tests.gitfixture import git, make_repo

LIB = "def greet():\n    return 'hi'\n"


class ScriptedWorker:
    """Runs `scripts[task.description](task, worktree, run_no)` -> files or globs."""

    def __init__(self, id, scripts, type="backend", cost=0.0, barrier=None, co_authors=(),
                 proposals=()):
        self.id, self.type, self.scripts, self.cost = id, type, scripts, cost
        self.barrier, self.co_authors, self.proposals = barrier, co_authors, proposals
        self.runs: list[tuple[int, str]] = []

    def run(self, task, worktree: Path) -> WorkerResult:
        self.runs.append((task.id, task.review_notes))
        if self.barrier:
            self.barrier.wait(timeout=10)  # only passes if the other worker runs too
        out = self.scripts[task.spec.description](task, worktree, len(self.runs))
        if isinstance(out, tuple):
            return WorkerResult(scope_request=out, cost_usd=self.cost)
        for name, text in out.items():
            (worktree / name).parent.mkdir(parents=True, exist_ok=True)
            (worktree / name).write_text(text)
        return WorkerResult(diff="(see worktree)", cost_usd=self.cost,
                            co_authors=self.co_authors, proposals=self.proposals)


class ScriptedReviewer:
    id = "reviewer"

    def __init__(self, verdicts=None):
        self.verdicts = list(verdicts or [])
        self.seen: list[int] = []

    def review(self, request: ReviewRequest) -> Verdict:
        self.seen.append(request.task.id)
        return self.verdicts.pop(0) if self.verdicts else Verdict(True, "lgtm")


def writes(files):
    return lambda task, wt, n: files


def build(tmp_path, workers, reviewer=None, budget=100.0, author=None):
    repo = make_repo(tmp_path, {"lib.py": LIB, "app.py": "from lib import greet\n"})
    store = TaskStore(tmp_path / "seam.sqlite")
    sandbox = ImportCheckSandbox()
    g = GitRepo(repo, author=author)
    lead = Lead(store, "g", Budget(budget))
    gate = ReviewGate(g, sandbox, reviewer or ScriptedReviewer(), scratch=tmp_path)
    merger = MergeEngine(store, g, sandbox, suite_cmd="pytest", scratch=tmp_path)
    orch = Orchestrator(store, lead, g, workers, gate, merger, tmp_path / "worktrees")
    return repo, store, lead, orch


def add(store, desc, scope, deps=(), test_cmd="pytest"):
    return store.create(TaskSpec(goal_id="g", type="backend", description=desc,
                                 file_scope=tuple(scope), blocked_by=tuple(deps),
                                 test_cmd=test_cmd))


def main_tree_passes(repo) -> bool:
    with tempfile.TemporaryDirectory() as d:
        GitRepo(repo).export("main", Path(d))
        return ImportCheckSandbox().run("pytest", Path(d), 10).ok


def test_v1_case1_disjoint_tasks_merge_in_parallel_zero_touch(tmp_path):
    barrier = threading.Barrier(2)
    scripts = {"shout": writes({"shout.py": "from lib import greet\n"}),
               "count": writes({"count.py": "N = 1\n"})}
    w1 = ScriptedWorker("w1", scripts, barrier=barrier)
    w2 = ScriptedWorker("w2", scripts, barrier=barrier)
    repo, store, _, orch = build(tmp_path, [w1, w2])
    a = add(store, "shout", ["shout.py"])
    b = add(store, "count", ["count.py"])

    report = orch.run()

    assert sorted(report.ids_in(State.MERGED)) == [a.id, b.id]
    assert {store.get(a.id).owner, store.get(b.id).owner} == {"w1", "w2"}  # both used
    assert {"shout.py", "count.py"} <= set(git(repo, "ls-tree", "--name-only", "main").split())
    assert main_tree_passes(repo)
    assert not (tmp_path / "worktrees" / f"task-{a.id}").exists()  # cleaned up
    # One author throughout; the trailers say which worker wrote each task.
    assert set(git(repo, "log", "--format=%an", "main~1..main").split("\n")) == {
        "SEAM merge engine"}
    for t in (a, b):
        owner = store.get(t.id).owner
        msgs = git(repo, "log", "--format=%B", "--grep", f"SEAM-Worker: {owner}", "main")
        assert f"task {t.id}:" in msgs and f"Merge task {t.id}:" in msgs


def test_v1_case2_semantic_conflict_escalates_never_merges_broken(tmp_path):
    scripts = {
        "rename greet to hello": writes({"lib.py": LIB.replace("greet", "hello"),
                                         "app.py": "from lib import hello\n"}),
        "add farewell using greet": writes({"farewell.py": "from lib import greet\n"}),
    }
    workers = [ScriptedWorker("w1", scripts), ScriptedWorker("w2", scripts)]
    repo, store, _, orch = build(tmp_path, workers)
    rename = add(store, "rename greet to hello", ["lib.py", "app.py"])
    farewell = add(store, "add farewell using greet", ["farewell.py"])

    report = orch.run()

    states = {t.id: t.state for t in report.tasks}
    # Exactly one merged; the other was caught at the merge-time full suite.
    assert sorted(states.values()) == sorted([State.MERGED, State.NEEDS_HUMAN])
    escalated = next(t for t in report.tasks if t.state is State.NEEDS_HUMAN)
    assert escalated.id in (rename.id, farewell.id)
    assert "semantic conflict" in escalated.review_notes
    assert main_tree_passes(repo)  # main never received the break


def test_changes_requested_reruns_same_worker_with_notes(tmp_path):
    def script(task, wt, n):
        return {"f.py": "V = 1\n"} if "missing V" in task.review_notes else {"f.py": "\n"}

    w = ScriptedWorker("w1", {"f": script})
    reviewer = ScriptedReviewer([Verdict(False, "missing V", cost_usd=0.1)])
    _, store, lead, orch = build(tmp_path, [w], reviewer)
    t = add(store, "f", ["f.py"])

    orch.run()

    final = store.get(t.id)
    assert (final.state, final.attempts) == (State.MERGED, 1)
    assert [notes for _, notes in w.runs] == ["", "missing V"]
    assert lead.budget.spent_usd == pytest.approx(0.1)
    # Every run is accounted: two worker runs, the paid rejection, then the approval
    # (the scripted approval costs nothing and reports no usage, so it reads as gate).
    assert [(r.role, r.outcome) for r in store.runs(t.id)] == [
        ("worker", "diff"), ("reviewer", "reject"), ("worker", "diff"), ("gate", "approve")]
    assert store.runs(t.id)[1].notes == "missing V"  # survives the later approval


def test_dependent_task_waits_for_dependency_to_merge(tmp_path):
    order = []

    def rec(name, files):
        def script(task, wt, n):
            order.append(name)
            return files
        return script

    scripts = {"base": rec("base", {"base.py": ""}), "top": rec("top", {"top.py": ""})}
    _, store, _, orch = build(tmp_path, [ScriptedWorker("w1", scripts),
                                         ScriptedWorker("w2", scripts)])
    base = add(store, "base", ["base.py"])
    top = add(store, "top", ["top.py"], deps=[base.id])

    report = orch.run()
    assert sorted(report.ids_in(State.MERGED)) == [base.id, top.id]
    assert order == ["base", "top"]


def test_scope_request_granted_then_work_continues(tmp_path):
    def script(task, wt, n):
        if "extra.py" not in task.spec.file_scope:
            return ("extra.py",)
        return {"main_part.py": "", "extra.py": ""}

    w = ScriptedWorker("w1", {"s": script})
    _, store, _, orch = build(tmp_path, [w])
    t = add(store, "s", ["main_part.py"])
    orch.run()
    final = store.get(t.id)
    assert final.state is State.MERGED
    assert final.spec.file_scope == ("main_part.py", "extra.py")
    assert [notes for _, notes in w.runs] == ["", ""]  # a grant is not a rejection


def test_denied_scope_blocks_then_unblocks_after_holder_merges(tmp_path):
    gate_open = threading.Event()

    def holder(task, wt, n):
        gate_open.wait(timeout=10)  # hold shared.py until the other has asked
        return {"shared.py": ""}

    def asker(task, wt, n):
        if "shared.py" not in task.spec.file_scope:
            gate_open.set()
            return ("shared.py",)
        return {"mine.py": "", "shared.py": "X = 1\n"}

    scripts = {"holder": holder, "asker": asker}
    _, store, _, orch = build(tmp_path, [ScriptedWorker("w1", scripts),
                                         ScriptedWorker("w2", scripts)])
    h = add(store, "holder", ["shared.py"])
    a = add(store, "asker", ["mine.py"])

    report = orch.run()
    assert sorted(report.ids_in(State.MERGED)) == [h.id, a.id]


def test_worker_that_keeps_crashing_escalates(tmp_path):
    def boom(task, wt, n):
        raise RuntimeError("model unavailable")

    w = ScriptedWorker("w1", {"x": boom})
    _, store, _, orch = build(tmp_path, [w])
    t = add(store, "x", ["x.py"])
    orch.run()
    final = store.get(t.id)
    assert (final.state, final.attempts) == (State.NEEDS_HUMAN, 3)
    assert "model unavailable" in final.review_notes
    assert len(w.runs) == 3


def test_budget_cap_stops_new_runs(tmp_path):
    scripts = {"a": writes({"a.py": ""}), "b": writes({"b.py": ""})}
    w = ScriptedWorker("w1", scripts, cost=0.6)
    _, store, lead, orch = build(tmp_path, [w], budget=0.5)
    add(store, "a", ["a.py"])
    b = add(store, "b", ["b.py"])

    report = orch.run()
    assert report.budget_exhausted and report.spent_usd == pytest.approx(0.6)
    assert len(w.runs) == 1
    assert store.get(b.id).state is State.OPEN  # never dispatched


def test_failed_runs_still_charge_the_budget(tmp_path):
    def boom(task, wt, n):
        raise WorkerFailed("rate limited", cost_usd=0.2)

    _, store, lead, orch = build(tmp_path, [ScriptedWorker("w1", {"x": boom})])
    add(store, "x", ["x.py"])
    orch.run()
    assert lead.budget.spent_usd == pytest.approx(0.6)  # three bounded attempts
    assert [(r.outcome, r.cost_usd) for r in store.runs()] == [("failed", 0.2)] * 3


def test_worker_that_keeps_asking_for_scope_escalates(tmp_path):
    def greedy(task, wt, n):
        return (f"more{n}.py",)  # every grant just prompts another request

    w = ScriptedWorker("w1", {"g": greedy})
    _, store, _, orch = build(tmp_path, [w])
    t = add(store, "g", ["g.py"])
    orch.run()
    final = store.get(t.id)
    assert (final.state, final.attempts) == (State.NEEDS_HUMAN, 3)
    assert final.spec.file_scope == ("g.py", "more1.py", "more2.py")  # third never granted
    assert "more3.py" in final.review_notes
    assert len(w.runs) == 3


def test_review_probes_are_recorded_with_the_run(tmp_path):
    from seam_merge.reviewer.base import Probe
    probe = Probe("test_probe_a.py", "def test_a():\n    assert True\n")
    reviewer = ScriptedReviewer([Verdict(True, "ok", cost_usd=0.01, probes=(probe,))])
    _, store, _, orch = build(tmp_path, [ScriptedWorker("w1", {"a": writes({"a.py": ""})})],
                              reviewer)
    t = add(store, "a", ["a.py"])
    orch.run()
    review = next(r for r in store.runs(t.id) if r.role == "reviewer")
    assert json.loads(review.probes) == [{"name": "test_probe_a.py", "code": probe.code}]


def test_scope_request_denied_before_a_restart_is_retried(tmp_path):
    """Run 1 left `asker` blocked on a denied request for shared.py; the holder was
    then cancelled. A fresh orchestrator (a restart) must retry the stored request,
    not leave the task blocked forever."""
    w = ScriptedWorker("w1", {"asker": writes({"mine.py": "", "shared.py": "X = 1\n"})})
    repo, store, _, orch = build(tmp_path, [w])  # its in-memory state knows nothing
    h = add(store, "holder", ["shared.py"])
    a = add(store, "asker", ["mine.py"])
    store.claim("w9", "backend")
    store.claim("w1", "backend")
    path = tmp_path / "worktrees" / f"task-{a.id}"
    GitRepo(repo).add_worktree(path, f"seam/task-{a.id}", "main")
    store.transition(a.id, Event.START, branch=f"seam/task-{a.id}", worktree_path=str(path))
    store.transition(a.id, Event.REQUEST_SCOPE)
    assert store.grant_scope(a.id, ["shared.py"]).state is State.BLOCKED
    store.transition(h.id, Event.CANCEL)

    report = orch.run()
    assert report.ids_in(State.MERGED) == [a.id]
    assert store.get(a.id).spec.file_scope == ("mine.py", "shared.py")


def test_budget_resumed_from_the_store_caps_a_restarted_run(tmp_path):
    from seam_merge.coordinator.lead import goal_budget
    from seam_merge.engine.usage import RunRecord, Usage

    w = ScriptedWorker("w1", {"a": writes({"a.py": ""})})
    _, store, lead, orch = build(tmp_path, [w])
    t = add(store, "a", ["a.py"])
    store.record_run(RunRecord(t.id, "worker", "w0", "failed", 1.0, Usage()))  # run 1
    lead.budget = goal_budget(store, 1.0, ["g"])

    report = orch.run()
    assert report.budget_exhausted and w.runs == []
    assert store.get(t.id).state is State.OPEN


def test_in_progress_task_of_an_absent_worker_is_adopted(tmp_path):
    """A restart with fewer workers: task left in_progress by `ghost`, who is not in
    this run. A free worker of the same type adopts it instead of leaving it stuck."""
    w = ScriptedWorker("w1", {"x": writes({"x.py": "X = 1\n"})})
    repo, store, _, orch = build(tmp_path, [w])
    t = add(store, "x", ["x.py"])
    store.claim("ghost", "backend")
    path = tmp_path / "worktrees" / f"task-{t.id}"
    GitRepo(repo).add_worktree(path, f"seam/task-{t.id}", "main")
    store.transition(t.id, Event.START, branch=f"seam/task-{t.id}", worktree_path=str(path))

    report = orch.run()
    assert report.ids_in(State.MERGED) == [t.id]
    assert store.get(t.id).owner == "w1" and len(w.runs) == 1

def test_human_rework_reaches_a_worker_of_a_later_run_and_merges(tmp_path):
    """Escalated after 3 rejections; a human reworks it with notes; the next run has
    a different worker, which adopts the task, gets the notes, and the task merges
    only after a fresh approving review."""
    from seam_merge.coordinator import escalation

    scripts = {"x": lambda task, wt, n: {"x.py": f"X = {n}\n"}}  # new content each run
    rejecting = ScriptedReviewer([Verdict(False, "no")] * 3)
    _, store, lead, orch = build(tmp_path, [ScriptedWorker("w1", scripts)], rejecting)
    t = add(store, "x", ["x.py"])
    orch.run()
    assert store.get(t.id).state is State.NEEDS_HUMAN
    assert [r.outcome for r in store.runs(t.id) if r.role != "worker"] == ["reject"] * 3
    with pytest.raises(escalation.EscalationError):
        escalation.resolve(store, t.id)  # failed review: only rework or cancel
    escalation.rework(store, t.id, "name it X")

    w2 = ScriptedWorker("w2", scripts)
    orch2 = Orchestrator(store, lead, orch.repo, [w2], orch.gate, orch.merger,
                         orch.worktree_root)
    orch2.gate.reviewer = ScriptedReviewer()  # approves
    report = orch2.run()
    assert report.ids_in(State.MERGED) == [t.id]
    assert w2.runs == [(t.id, "From a human: name it X")]
    assert store.get(t.id).owner == "w2"


def test_untestable_task_escalates_once_then_rereview_merges_it(tmp_path):
    """The funda-cli case: the sandbox lacks the task's test tool. The task must
    escalate after ONE worker run (no retries the worker can't fix), and once the
    sandbox is fixed, `rereview` must get the same branch merged without rework."""
    from seam_merge.coordinator import escalation
    from seam_merge.sandbox.base import RunResult

    class Sandbox(ImportCheckSandbox):
        has_go = False

        def run(self, cmd, workdir, timeout):
            if cmd.startswith("go ") and not self.has_go:
                return RunResult(127, "sh: 1: go: not found")
            return super().run("pytest", workdir, timeout)

    sandbox = Sandbox()
    w = ScriptedWorker("w1", {"x": writes({"x.py": "X = 1\n"})})
    repo = make_repo(tmp_path, {"lib.py": LIB, "app.py": "from lib import greet\n"})
    store = TaskStore(tmp_path / "seam.sqlite")
    g = GitRepo(repo)
    reviewer = ScriptedReviewer()
    orch = Orchestrator(store, Lead(store, "g", Budget(100)), g, [w],
                        ReviewGate(g, sandbox, reviewer, scratch=tmp_path),
                        MergeEngine(store, g, sandbox, suite_cmd="pytest", scratch=tmp_path),
                        tmp_path / "worktrees")
    t = add(store, "x", ["x.py"], test_cmd="go test ./...")

    orch.run()
    got = store.get(t.id)
    assert (got.state, got.attempts) == (State.NEEDS_HUMAN, 0)
    assert len(w.runs) == 1 and reviewer.seen == []  # no worker retries, no model paid
    assert "could not run in the sandbox" in got.review_notes

    sandbox.has_go = True  # the human builds an image with Go
    escalation.rereview(store, t.id, "image fixed")
    orch.run()
    assert store.get(t.id).state is State.MERGED
    assert len(w.runs) == 1  # the same branch, never reworked


def test_operator_authors_seam_commits_and_models_are_credited(tmp_path):
    operator = Identity("Ada Operator", "ada@example.com")
    model = Identity("claude-opus-5-5", "noreply@anthropic.com")
    w = ScriptedWorker("w1", {"count": writes({"count.py": "N = 1\n"})}, co_authors=(model,))
    repo, store, _, orch = build(tmp_path, [w], author=operator)
    t = add(store, "count", ["count.py"])

    assert orch.run().ids_in(State.MERGED) == [t.id]
    fmt = "--format=%an <%ae>|%cn <%ce>|%(trailers:key=Co-Authored-By,valueonly,separator=;)"
    seam = "SEAM merge engine <seam@localhost>"
    # The merge commit and, as its second parent, the worker's task commit.
    assert git(repo, "log", "-1", fmt, "main").split("|") == [str(operator), seam, ""]
    assert git(repo, "log", "-1", fmt, "main^2").split("|") == [str(operator), seam, str(model)]



def test_proposals_are_kept_with_the_task_and_reach_the_reviewer(tmp_path):
    class SeeingReviewer(ScriptedReviewer):
        def review(self, request):
            self.proposals = request.task.proposals
            return super().review(request)

    reviewer = SeeingReviewer()
    w = ScriptedWorker("w1", {"count": writes({"count.py": "N = 1\n"})},
                       proposals=("lib.greet mishandles empty names",))
    _, store, _, orch = build(tmp_path, [w], reviewer=reviewer)
    t = add(store, "count", ["count.py"])

    assert orch.run().ids_in(State.MERGED) == [t.id]
    assert store.get(t.id).proposals == ("lib.greet mishandles empty names",)
    assert reviewer.proposals == ("lib.greet mishandles empty names",)
    assert store.list(State.OPEN) == []  # a proposal is not a task (invariant 7)


# -- task branches: one namespace per state dir ------------------------------------------

def test_a_leftover_seam_branch_does_not_strand_the_task(tmp_path):
    """Regression: `seam/task-1` from an earlier run (another state dir, or an older
    SEAM) made `git worktree add -b` fail and left the task stuck in `claimed`."""
    w = ScriptedWorker("w1", {"count": writes({"count.py": "N = 1\n"})})
    repo, store, _, orch = build(tmp_path, [w])
    git(repo, "branch", "seam/task-1", "main")
    t = add(store, "count", ["count.py"])
    assert orch.run().ids_in(State.MERGED) == [t.id]


def test_two_state_dirs_on_one_repo_never_share_a_branch(tmp_path):
    repo, first, _, orch = build(tmp_path, [ScriptedWorker("w1", {
        "count": writes({"count.py": "N = 1\n"})})])
    add(first, "count", ["count.py"])
    assert orch.run().ids_in(State.MERGED) == [1]

    second = TaskStore(tmp_path / "other.sqlite")
    g = GitRepo(repo)
    sandbox = ImportCheckSandbox()
    orch2 = Orchestrator(second, Lead(second, "g", Budget(100)), g,
                         [ScriptedWorker("w1", {"more": writes({"more.py": "M = 1\n"})})],
                         ReviewGate(g, sandbox, ScriptedReviewer(), scratch=tmp_path),
                         MergeEngine(second, g, sandbox, suite_cmd="pytest", scratch=tmp_path),
                         tmp_path / "worktrees2")
    t2 = add(second, "more", ["more.py"])  # task 1 again, in its own state dir
    assert orch2.run().ids_in(State.MERGED) == [t2.id]
    assert first.branch_namespace != second.branch_namespace
    assert {"count.py", "more.py"} <= set(git(repo, "ls-tree", "--name-only", "main").split())


def test_merged_branches_are_deleted_and_escalated_ones_kept(tmp_path):
    scripts = {
        "rename greet to hello": writes({"lib.py": LIB.replace("greet", "hello"),
                                         "app.py": "from lib import hello\n"}),
        "add farewell using greet": writes({"farewell.py": "from lib import greet\n"}),
    }
    repo, store, _, orch = build(tmp_path, [ScriptedWorker("w1", scripts),
                                            ScriptedWorker("w2", scripts)])
    add(store, "rename greet to hello", ["lib.py", "app.py"])
    add(store, "add farewell using greet", ["farewell.py"])
    orch.run()
    (merged,) = store.list(State.MERGED)
    (escalated,) = store.list(State.NEEDS_HUMAN)
    assert merged.branch.startswith(f"seam/{store.branch_namespace}/task-")
    assert not GitRepo(repo).has_branch(merged.branch)
    assert GitRepo(repo).has_branch(escalated.branch)  # a human may need to inspect it
