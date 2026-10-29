import pytest

from engine.git import GitRepo
from engine.states import State
from engine.task import Task, TaskSpec
from reviewer.base import Probe, ReviewRequest, Verdict
from reviewer.gate import ReviewerNotIndependent, ReviewGate
from sandbox.base import RunResult
from tests.fakes import ImportCheckSandbox
from tests.gitfixture import branch_with, git, make_repo

LGTM = Verdict(True, "lgtm")


class RecordingReviewer:
    def __init__(self, id="rev", verdict=LGTM):
        self.id, self.verdict, self.requests = id, verdict, []

    def review(self, request: ReviewRequest) -> Verdict:
        self.requests.append(request)
        return self.verdict


def task(branch, scope, test_cmd="pytest", owner="w1"):
    spec = TaskSpec(goal_id="g", type="backend", description="d",
                    file_scope=tuple(scope), test_cmd=test_cmd)
    return Task(id=1, spec=spec, state=State.IN_REVIEW, owner=owner, branch=branch,
                diff="worker's own claimed diff — must be ignored")


@pytest.fixture
def setup(tmp_path):
    repo = make_repo(tmp_path, {"lib.py": "def greet():\n    pass\n"})
    reviewer = RecordingReviewer()
    sandbox = ImportCheckSandbox()
    return repo, reviewer, sandbox, ReviewGate(GitRepo(repo), sandbox, reviewer)


def test_in_scope_passing_task_goes_to_reviewer_with_git_diff(setup):
    repo, reviewer, sandbox, gate = setup
    branch_with(repo, "t", {"feat/x.py": "from lib import greet\n"})
    assert gate.review(task("t", ["feat/**"])) == Verdict(True, "lgtm")
    (req,) = reviewer.requests
    assert "+from lib import greet" in req.diff
    assert "claimed" not in req.diff  # the git diff, not the worker's claim
    assert req.test_output == "all passed"
    assert sandbox.calls[0][0] == "pytest"


def test_out_of_scope_edit_rejected_before_tests_or_reviewer(setup):
    repo, reviewer, sandbox, gate = setup
    branch_with(repo, "t", {"feat/x.py": "", "lib.py": "def other(): pass\n"})
    v = gate.review(task("t", ["feat/**"]))
    assert not v.approve and "outside file_scope" in v.notes and "lib.py" in v.notes
    assert sandbox.calls == [] and reviewer.requests == []


def test_empty_diff_rejected(setup):
    repo, reviewer, _, gate = setup
    branch_with(repo, "t", {})
    assert not gate.review(task("t", ["**"])).approve
    assert reviewer.requests == []


def test_failing_task_tests_rejected_without_reviewer(setup):
    repo, reviewer, _, gate = setup
    branch_with(repo, "t", {"feat/x.py": "from lib import nope\n"})
    v = gate.review(task("t", ["feat/**"]))
    assert not v.approve and "cannot import name 'nope'" in v.notes
    assert reviewer.requests == []


def test_reviewer_rejection_passes_through(setup):
    repo, reviewer, _, gate = setup
    reviewer.verdict = Verdict(False, "does not implement the spec", cost_usd=0.02)
    branch_with(repo, "t", {"feat/x.py": ""})
    assert gate.review(task("t", ["feat/**"])) == reviewer.verdict


def test_reviewer_must_not_be_author(setup):
    repo, _, _, gate = setup
    branch_with(repo, "t", {"feat/x.py": ""})
    with pytest.raises(ReviewerNotIndependent):
        gate.review(task("t", ["feat/**"], owner="rev"))


def test_reviewer_gets_changed_files_as_they_were_at_the_fork_point(setup):
    repo, reviewer, _, gate = setup
    branch_with(repo, "t", {"lib.py": "def greet():\n    return 1\n", "feat/new.py": ""})
    # main moves on after the fork; the original must be what the diff applies to.
    git(repo, "update-ref", "refs/heads/main",
        branch_with(repo, "side", {"lib.py": "def greet():\n    return 2\n"}))
    gate.review(task("t", ["lib.py", "feat/**"]))
    (req,) = reviewer.requests
    assert req.files == {"lib.py": "def greet():\n    pass\n"}  # new files have no original
    assert req.files_omitted == ()


def test_original_context_is_bounded_smallest_first_and_skips_binaries(tmp_path):
    repo = make_repo(tmp_path, {"lib.py": "def greet(): pass\n", "big.py": "x" * 500,
                                "small.py": "s = 1\n", "blob.bin": "a\0b"})
    reviewer = RecordingReviewer()
    gate = ReviewGate(GitRepo(repo), ImportCheckSandbox(), reviewer, context_chars=100)
    branch_with(repo, "t", {"big.py": "y", "small.py": "s = 2\n", "blob.bin": "c"})
    gate.review(task("t", ["*.py", "*.bin"]))
    (req,) = reviewer.requests
    assert req.files == {"small.py": "s = 1\n"}
    assert req.files_omitted == ("big.py", "blob.bin")


def test_large_files_are_whole_by_default_and_focused_on_request(tmp_path):
    from reviewer import context
    big = "\n".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(2000))
    assert len(big) > context.WHOLE_FILE_CHARS
    repo = make_repo(tmp_path, {"lib.py": "def greet(): pass\n", "big.py": big})
    branch_with(repo, "t", {"big.py": big.replace("x + 7\n", "x + 70\n", 1)})
    for focused, expect_whole in ((False, True), (True, False)):
        reviewer = RecordingReviewer()
        ReviewGate(GitRepo(repo), ImportCheckSandbox(), reviewer,
                   focused_context=focused).review(task("t", ["big.py"]))
        text = reviewer.requests[0].files["big.py"]
        assert (text == big) is expect_whole
        assert "return x + 7" in text  # the touched definition is there either way


class ProbeSandbox(ImportCheckSandbox):
    """Like ImportCheckSandbox, but the probe command returns `probe_result` and the
    probe files it was given are recorded (nothing is executed)."""

    def __init__(self, probe_result):
        super().__init__()
        self.probe_result, self.probe_files = probe_result, None

    def run(self, cmd, workdir, timeout):
        if "seam_probes" in cmd:
            self.probe_files = {p.name: p.read_text() for p in (workdir / "seam_probes").iterdir()}
            self.calls.append((cmd, workdir, []))
            return self.probe_result
        return super().run(cmd, workdir, timeout)


def probe_gate(tmp_path, probe_result, verdict, probe_cmd="python -m pytest seam_probes"):
    repo = make_repo(tmp_path, {"lib.py": "def greet(): pass\n"})
    branch_with(repo, "t", {"feat/x.py": "X = 1\n"})
    sandbox = ProbeSandbox(probe_result)
    gate = ReviewGate(GitRepo(repo), sandbox, RecordingReviewer(verdict=verdict),
                      probe_cmd=probe_cmd)
    return gate, sandbox


PROBE = Probe("test_probe_x.py", "from feat.x import X\ndef test_x():\n    assert X == 2\n")


@pytest.mark.parametrize("exit_code", [1, 2])
def test_a_failing_probe_always_rejects_with_the_evidence(tmp_path, exit_code):
    gate, sandbox = probe_gate(tmp_path, RunResult(exit_code, "E  assert 1 == 2"),
                               Verdict(True, "looks fine", probes=(PROBE,)))
    v = gate.review(task("t", ["feat/**"]))
    assert not v.approve
    assert "assert X == 2" in v.notes and "E  assert 1 == 2" in v.notes
    assert "looks fine" in v.notes  # the reviewer's own view is kept for the author
    assert sandbox.probe_files == {"test_probe_x.py": PROBE.code}


@pytest.mark.parametrize("result, word", [
    (RunResult(0, "1 passed"), "passed"),
    (RunResult(5, "no tests ran"), "broken"),
    (RunResult(-1, "", timed_out=True), "timed out"),
])
def test_passing_or_broken_probes_leave_the_verdict(tmp_path, result, word):
    gate, _ = probe_gate(tmp_path, result, Verdict(True, "ok", probes=(PROBE,)))
    v = gate.review(task("t", ["feat/**"]))
    assert v.approve and f"[probes {word}" in v.notes


def test_a_rejection_stays_a_rejection_when_probes_pass(tmp_path):
    gate, _ = probe_gate(tmp_path, RunResult(0, ""), Verdict(False, "no tests", probes=(PROBE,)))
    assert not gate.review(task("t", ["feat/**"])).approve


def test_invalid_python_probes_are_dropped_unrun(tmp_path):
    bad = Probe("test_probe_bad.py", "def test(:\n")
    gate, sandbox = probe_gate(tmp_path, RunResult(1, "would reject"),
                               Verdict(True, "ok", probes=(bad,)))
    v = gate.review(task("t", ["feat/**"]))
    assert v.approve and sandbox.probe_files is None and "invalid" in v.notes


def test_probes_are_ignored_without_a_probe_cmd(tmp_path):
    gate, sandbox = probe_gate(tmp_path, RunResult(1, "would reject"),
                               Verdict(True, "ok", probes=(PROBE,)), probe_cmd=None)
    assert gate.review(task("t", ["feat/**"])).approve and sandbox.probe_files is None
