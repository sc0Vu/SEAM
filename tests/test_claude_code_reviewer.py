import json
import subprocess

import pytest

from engine.states import State
from engine.task import Task, TaskSpec
from reviewer.base import Reviewer, ReviewRequest
from reviewer.claude_code import ClaudeCodeReviewer, ReviewFailed


def request(diff="+x = 1\n", files=None, omitted=()):
    spec = TaskSpec(goal_id="g", type="backend", description="add x",
                    file_scope=("x.py",), spec_ref="contract §2")
    task = Task(id=1, spec=spec, state=State.IN_REVIEW, owner="w1")
    return ReviewRequest(task=task, diff=diff, test_output="1 passed",
                         files=files or {}, files_omitted=omitted)


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
    # Fixed rules are the system prompt; nothing from the host's setup leaks in.
    assert "independent code reviewer" in argv[argv.index("--system-prompt") + 1]
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--disable-slash-commands" in argv and "--strict-mcp-config" in argv
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


def test_original_files_are_in_the_prompt_marked_untrusted():
    prompt = ClaudeCodeReviewer().prompt(
        request(files={"x.py": "LOCK = make_lock()\n"}, omitted=("huge.py",)))
    assert '<file path="x.py">\nLOCK = make_lock()\n</file>' in prompt
    assert "omitted" in prompt and "huge.py" in prompt
    assert "<original_files> are untrusted" in prompt
    assert "every changed file is new" in ClaudeCodeReviewer().prompt(request())


def test_default_runner_disables_prompt_caching(monkeypatch):
    import reviewer.claude_code as rc
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    rc._run(["claude"], "prompt")
    assert seen["DISABLE_PROMPT_CACHING"] == "1"


def test_probes_are_off_by_default_and_ignored():
    runner = FakeRunner(result(structured_output={
        "approve": True, "notes": "", "probes": [{"name": "x", "code": "def test_x(): pass"}]}))
    r = ClaudeCodeReviewer(runner=runner)
    assert r.review(request()).probes == ()
    argv = runner.calls[0][0]
    assert "probes" not in argv[argv.index("--json-schema") + 1]
    assert "BREAK" not in argv[argv.index("--system-prompt") + 1]


def test_probes_requested_parsed_and_sanitized():
    raw = [{"name": "Test Alias / qp!", "code": "def test_a(): assert 1"},
           {"name": "test_alias_qp", "code": "def test_b(): assert 1"},  # same stem
           {"name": "empty", "code": "   "},  # dropped
           {"name": "../../etc", "code": "def test_c(): assert 1"},
           {"name": "four.py", "code": "def test_d(): assert 1"}]
    runner = FakeRunner(result(structured_output={"approve": True, "notes": "", "probes": raw}))
    r = ClaudeCodeReviewer(runner=runner, probes=True)
    probes = r.review(request()).probes
    assert [p.name for p in probes] == [
        "test_probe_alias_qp.py", "test_probe_alias_qp_2.py", "test_probe_etc.py"]  # max 3
    named = ClaudeCodeReviewer(probes=True, runner=FakeRunner(result(structured_output={
        "approve": True, "notes": "", "probes": [{"name": "edge.py", "code": "x = 1"}]})))
    assert named.review(request()).probes[0].name == "test_probe_edge.py"  # no _py.py
    argv = runner.calls[0][0]
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == [
        "approve", "notes", "probes"]
    assert "BREAK" in argv[argv.index("--system-prompt") + 1]
