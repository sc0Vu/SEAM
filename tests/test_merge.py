"""Merge-engine tests on real throwaway git repos.

The sandbox is a fake that *inspects* the exported tree instead of executing it, so
no fixture code runs on the host. Its "suite" is: every `from lib import NAME` in any
file must name something `lib.py` defines — enough to express a semantic conflict.
"""

import re
from pathlib import Path

import pytest

from engine.errors import TargetCheckedOut
from engine.git import GitRepo
from engine.merge import MergeEngine
from engine.states import Event, State
from engine.task import TaskSpec
from sandbox.base import RunResult
from store import TaskStore
from tests.gitfixture import branch_with, commit_files, git, make_repo

LIB = "def greet():\n    return 'hi'\n"


class ImportCheckSandbox:
    """Fake sandbox: passes iff every `from lib import X` resolves in lib.py."""

    def __init__(self, during=None):
        self.calls = []
        self.during = during  # hook to simulate something happening mid-suite

    def run(self, cmd: str, workdir: Path, timeout: float) -> RunResult:
        self.calls.append((cmd, workdir, sorted(p.name for p in workdir.iterdir())))
        if self.during:
            self.during()
        lib = (workdir / "lib.py").read_text()
        defined = set(re.findall(r"^def (\w+)", lib, re.M))
        for f in workdir.glob("*.py"):
            for name in re.findall(r"from lib import (\w+)", f.read_text()):
                if name not in defined:
                    return RunResult(1, f"ImportError: cannot import name {name!r} ({f.name})")
        return RunResult(0, "all passed")


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path, {"lib.py": LIB, "app.py": "from lib import greet\n"})
    store = TaskStore(tmp_path / "seam.sqlite")
    sandbox = ImportCheckSandbox()
    engine = MergeEngine(store, GitRepo(repo), sandbox, suite_cmd="pytest", scratch=tmp_path)
    yield repo, store, sandbox, engine
    store.close()


def approved_task(store, repo, branch, scope, files, priority=0):
    """A task that went claim -> review -> approve, with its branch committed."""
    branch_with(repo, branch, files)
    t = store.create(TaskSpec(goal_id="g", type="backend", description=branch,
                              file_scope=tuple(scope), priority=priority))
    store.claim("w", "backend")
    store.transition(t.id, Event.START, branch=branch)
    for e in (Event.SUBMIT, Event.PICKUP, Event.APPROVE, Event.ENQUEUE):
        store.transition(t.id, e)
    return store.get(t.id)


def main_files(repo):
    return set(git(repo, "ls-tree", "--name-only", "main").splitlines())


def test_disjoint_tasks_both_merge_green(env):
    """Engine half of v1 case 1: two disjoint-scope tasks merge with zero human touch."""
    repo, store, sandbox, engine = env
    a = approved_task(store, repo, "seam/a", ["feature_a.py"], {"feature_a.py": "A = 1\n"})
    b = approved_task(store, repo, "seam/b", ["feature_b.py"], {"feature_b.py": "B = 1\n"})

    assert engine.merge_next().state is State.MERGED
    assert engine.merge_next().state is State.MERGED
    assert engine.merge_next() is None

    assert {"feature_a.py", "feature_b.py"} <= main_files(repo)
    head_parents = git(repo, "rev-list", "--parents", "-n1", "main").split()[1:]
    assert len(head_parents) == 2  # a real merge commit
    assert [store.get(t.id).state for t in (a, b)] == [State.MERGED, State.MERGED]
    # The suite ran once per merge, on an exported tree with no .git.
    assert [c[0] for c in sandbox.calls] == ["pytest", "pytest"]
    assert all(".git" not in c[2] for c in sandbox.calls)


def test_semantic_conflict_escalates_and_main_stays_green(env):
    """Engine half of v1 case 2: each branch is fine alone; merged they break."""
    repo, store, _, engine = env
    rename = approved_task(
        store, repo, "seam/rename", ["lib.py", "app.py"],
        {"lib.py": LIB.replace("greet", "hello"), "app.py": "from lib import hello\n"},
    )
    caller = approved_task(store, repo, "seam/caller", ["extra.py"],
                           {"extra.py": "from lib import greet\n"})

    assert engine.merge_next().state is State.MERGED
    main_after_rename = git(repo, "rev-parse", "main")

    escalated = engine.merge_next()
    assert escalated.id == caller.id
    assert escalated.state is State.NEEDS_HUMAN
    assert "semantic conflict" in escalated.review_notes
    assert "cannot import name 'greet'" in escalated.review_notes
    assert git(repo, "rev-parse", "main") == main_after_rename  # break never merged
    assert store.get(rename.id).state is State.MERGED


def test_textual_conflict_escalates_without_running_suite(env):
    repo, store, sandbox, engine = env
    t = approved_task(store, repo, "seam/t", ["lib.py"], {"lib.py": "def greet(): return 1\n"})
    # Someone outside SEAM edits the same line on main.
    wt = repo.parent / "outside"
    git(repo, "worktree", "add", "-q", str(wt), "main")
    commit_files(wt, {"lib.py": "def greet(): return 2\n"})
    git(repo, "worktree", "remove", "--force", str(wt))
    main_before = git(repo, "rev-parse", "main")

    got = engine.merge_next()
    assert (got.id, got.state) == (t.id, State.NEEDS_HUMAN)
    assert "textual conflict" in got.review_notes and "lib.py" in got.review_notes
    assert git(repo, "rev-parse", "main") == main_before
    assert sandbox.calls == []


def test_queue_order_is_priority_then_id(env):
    repo, store, _, engine = env
    low = approved_task(store, repo, "seam/low", ["l.py"], {"l.py": ""}, priority=0)
    high = approved_task(store, repo, "seam/high", ["h.py"], {"h.py": ""}, priority=9)
    assert [engine.merge_next().id, engine.merge_next().id] == [high.id, low.id]


def test_target_moved_mid_suite_requeues_without_clobbering(env, tmp_path):
    repo, store, sandbox, engine = env
    t = approved_task(store, repo, "seam/t", ["t.py"], {"t.py": ""})
    outside = branch_with(repo, "outside", {"o.py": ""})
    sandbox.during = lambda: git(repo, "update-ref", "refs/heads/main", outside)

    got = engine.merge_next()
    assert (got.id, got.state) == (t.id, State.QUEUED_FOR_MERGE)
    assert git(repo, "rev-parse", "main") == outside  # the outside commit survives

    sandbox.during = None
    assert engine.merge_next().state is State.MERGED
    assert {"t.py", "o.py"} <= main_files(repo)


def test_unexpected_error_before_publish_requeues_and_reraises(env):
    repo, store, sandbox, engine = env
    t = approved_task(store, repo, "seam/t", ["t.py"], {"t.py": ""})
    main_before = git(repo, "rev-parse", "main")

    def boom():
        raise RuntimeError("sandbox died")

    sandbox.during = boom
    with pytest.raises(RuntimeError):
        engine.merge_next()
    assert store.get(t.id).state is State.QUEUED_FOR_MERGE  # slot released
    assert git(repo, "rev-parse", "main") == main_before


def _crash_on(monkeypatch, obj, method):
    def die(*a, **kw):
        raise KeyboardInterrupt("simulated crash")
    monkeypatch.setattr(obj, method, die)


def test_crash_after_publish_recovers_to_merged(env, tmp_path, monkeypatch):
    repo, store, sandbox, engine = env
    t = approved_task(store, repo, "seam/t", ["t.py"], {"t.py": ""})
    real = store.transition

    def transition(task_id, event, **kw):
        if event is Event.MERGE_CLEAN:
            raise KeyboardInterrupt("simulated crash")
        return real(task_id, event, **kw)

    monkeypatch.setattr(store, "transition", transition)
    with pytest.raises(KeyboardInterrupt):
        engine.merge_next()
    assert "t.py" in main_files(repo)  # published...
    assert store.get(t.id).state is State.MERGING  # ...but not marked

    restarted = TaskStore(tmp_path / "seam.sqlite")
    fresh = MergeEngine(restarted, GitRepo(repo), sandbox, suite_cmd="pytest")
    assert [(r.id, r.state) for r in fresh.recover()] == [(t.id, State.MERGED)]
    restarted.close()


def test_crash_before_publish_recovers_to_queued(env, tmp_path, monkeypatch):
    repo, store, sandbox, engine = env
    t = approved_task(store, repo, "seam/t", ["t.py"], {"t.py": ""})
    main_before = git(repo, "rev-parse", "main")
    _crash_on(monkeypatch, engine.repo, "update_branch")  # recorded, not yet published
    with pytest.raises(KeyboardInterrupt):
        engine.merge_next()
    assert store.get(t.id).state is State.MERGING

    restarted = TaskStore(tmp_path / "seam.sqlite")
    fresh = MergeEngine(restarted, GitRepo(repo), sandbox, suite_cmd="pytest")
    assert [(r.id, r.state) for r in fresh.recover()] == [(t.id, State.QUEUED_FOR_MERGE)]
    assert git(repo, "rev-parse", "main") == main_before
    restarted.close()


def test_refuses_when_target_checked_out(env):
    repo, store, _, engine = env
    t = approved_task(store, repo, "seam/t", ["t.py"], {"t.py": ""})
    git(repo, "checkout", "-q", "main")
    with pytest.raises(TargetCheckedOut):
        engine.merge_next()
    assert store.get(t.id).state is State.QUEUED_FOR_MERGE
