import json
import subprocess

import pytest

from engine.states import State
from engine.task import Task, TaskSpec
from reviewer.base import Reviewer, ReviewRequest
from reviewer.claude_code import ClaudeCodeReviewer, ReviewFailed


def request(diff="+x = 1\n"):
    spec = TaskSpec(goal_id="g", type="backend", description="add x",
                    file_scope=("x.py",), spec_ref="contract §2")
    task = Task(id=1, spec=spec, state=State.IN_REVIEW, owner="w1")
    return ReviewRequest(task=task, diff=diff, test_output="1 passed")


class FakeRunner:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode = stdout, returncode
        self.calls = []

    def __call__(self, argv, prompt):
        self.calls.append((argv, prompt))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "err")


def result(**kw):
    return json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.03, **kw})


def test_is_a_reviewer():
    assert isinstance(ClaudeCodeReviewer(), Reviewer)


def test_structured_verdict_parsed():
    runner = FakeRunner(result(structured_output={"approve": False, "notes": "no tests"},
                               usage={"input_tokens": 2000, "output_tokens": 40}))
    v = ClaudeCodeReviewer(runner=runner).review(request())
    assert (v.approve, v.notes, v.cost_usd) == (False, "no tests", 0.03)
    assert (v.usage.input_tokens, v.usage.output_tokens) == (2000, 40)


def test_runs_with_no_tools_and_schema_and_diff_on_stdin():
    runner = FakeRunner(result(structured_output={"approve": True, "notes": ""}))
    ClaudeCodeReviewer(runner=runner, model="m").review(request())
    argv, prompt = runner.calls[0]
    assert argv[argv.index("--tools") + 1] == ""
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == ["approve", "notes"]
    assert "+x = 1" not in " ".join(argv)  # the diff never goes through argv
    assert "+x = 1" in prompt and "contract §2" in prompt and "untrusted" in prompt


def test_huge_diff_truncated_with_reject_hint():
    prompt = ClaudeCodeReviewer().prompt(request(diff="+" + "y" * 300_000))
    assert "truncated" in prompt and len(prompt) < 210_000


@pytest.mark.parametrize("stdout", [
    "not json at all",
    result(is_error=True, result="overloaded"),
    result(result="forgot the schema"),
])
def test_no_verdict_is_a_review_failure(stdout):
    with pytest.raises(ReviewFailed):
        ClaudeCodeReviewer(runner=FakeRunner(stdout)).review(request())
