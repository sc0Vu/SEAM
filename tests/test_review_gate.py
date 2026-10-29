import pytest

from engine.git import GitRepo
from engine.states import State
from engine.task import Task, TaskSpec
from reviewer.base import ReviewRequest, Verdict
from reviewer.gate import ReviewerNotIndependent, ReviewGate
from tests.fakes import ImportCheckSandbox
from tests.gitfixture import branch_with, make_repo

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
