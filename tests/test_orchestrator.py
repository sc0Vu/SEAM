"""End-to-end orchestrator runs on real throwaway git repos.

Workers are scripted (they write files into their worktree), the reviewer is scripted,
and the sandbox is the inspect-only fake, so these exercise the engine's integration
logic without any model calls. They are the engine-level regression tests for the two
v1 cases; the real-runtime run is examples/greeter (see its README).
"""

import tempfile
import threading
from pathlib import Path

import pytest

from coordinator.lead import Budget, Lead
from engine.git import GitRepo
from engine.merge import MergeEngine
from engine.orchestrator import Orchestrator
from engine.states import State
from engine.task import TaskSpec
from reviewer.base import ReviewRequest, Verdict
from reviewer.gate import ReviewGate
from store import TaskStore
from tests.fakes import ImportCheckSandbox
from tests.gitfixture import git, make_repo
from workers.base import WorkerFailed, WorkerResult

LIB = "def greet():\n    return 'hi'\n"


class ScriptedWorker:
    """Runs `scripts[task.description](task, worktree, run_no)` -> files or globs."""

    def __init__(self, id, scripts, type="backend", cost=0.0, barrier=None):
        self.id, self.type, self.scripts, self.cost = id, type, scripts, cost
        self.barrier = barrier
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
        return WorkerResult(diff="(see worktree)", cost_usd=self.cost)


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


def build(tmp_path, workers, reviewer=None, budget=100.0):
    repo = make_repo(tmp_path, {"lib.py": LIB, "app.py": "from lib import greet\n"})
    store = TaskStore(tmp_path / "seam.sqlite")
    sandbox = ImportCheckSandbox()
    g = GitRepo(repo)
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
