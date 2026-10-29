import json
import shlex
from pathlib import Path

import pytest

from engine.git import GitRepo
from engine.states import State
from engine.task import Task, TaskSpec
from engine.usage import Usage
from sandbox.base import RunResult
from tests.gitfixture import make_repo
from workers import Worker, WorkerFailed
from workers.claude_code import (
    SCOPE_REQUEST_FILE,
    ClaudeCodeWorker,
    parse_result_json,
    usage_from_result,
)


def result_line(**kw):
    return json.dumps({"type": "result", "is_error": False, "result": "done",
                       "total_cost_usd": 0.25, **kw})


class FakeClaudeSandbox:
    """Pretends to be the container: applies `effect(workdir)`, prints a result."""

    def __init__(self, effect=None, line=None, exit_code=0):
        self.effect, self.line, self.exit_code = effect, line or result_line(), exit_code
        self.cmds = []

    def run(self, cmd, workdir: Path, timeout):
        self.cmds.append(cmd)
        if self.effect:
            self.effect(workdir)
        return RunResult(self.exit_code, "some log noise\n" + self.line + "\n")


def task(notes=""):
    spec = TaskSpec(goal_id="g", type="backend", description="add shout()",
                    file_scope=("shout.py", "test_shout.py"), test_cmd="pytest -q")
    return Task(id=7, spec=spec, state=State.IN_PROGRESS, owner="w1", review_notes=notes)


@pytest.fixture
def worktree(tmp_path):
    repo = make_repo(tmp_path, {"lib.py": "X = 1\n"})
    wt = tmp_path / "wt"
    GitRepo(repo).add_worktree(wt, "seam/task-7", "main")
    return wt


def test_is_a_worker():
    assert isinstance(ClaudeCodeWorker("w1", "backend", FakeClaudeSandbox()), Worker)


def test_edits_become_a_diff_with_cost(worktree):
    sb = FakeClaudeSandbox(effect=lambda wd: (wd / "shout.py").write_text("S = 1\n"))
    res = ClaudeCodeWorker("w1", "backend", sb).run(task(), worktree)
    assert "+S = 1" in res.diff and res.cost_usd == 0.25


def test_command_is_headless_sandboxed_and_scoped():
    w = ClaudeCodeWorker("w1", "backend", FakeClaudeSandbox(), model="m", max_budget_usd=2)
    argv = shlex.split(w.command(task()))
    assert argv[:2] == ["claude", "-p"]
    for flag in ("--output-format", "--dangerously-skip-permissions", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "2"
    assert argv[argv.index("--model") + 1] == "m"
    prompt = argv[2]
    assert "shout.py, test_shout.py" in prompt and SCOPE_REQUEST_FILE in prompt
    assert "REJECTED" not in prompt


def test_rework_prompt_carries_reviewer_feedback():
    prompt = ClaudeCodeWorker("w1", "backend", FakeClaudeSandbox()).prompt(task("needs tests"))
    assert "REJECTED" in prompt and "needs tests" in prompt


def test_scope_request_file_becomes_request_and_is_removed(worktree):
    def ask(wd):
        (wd / SCOPE_REQUEST_FILE).write_text("lib.py\n\nutil/**\n")

    res = ClaudeCodeWorker("w1", "backend", FakeClaudeSandbox(effect=ask)).run(task(), worktree)
    assert res.scope_request == ("lib.py", "util/**")
    assert not (worktree / SCOPE_REQUEST_FILE).exists()


@pytest.mark.parametrize(
    "sandbox, msg",
    [
        (FakeClaudeSandbox(), "changed nothing"),
        (FakeClaudeSandbox(line=result_line(is_error=True, result="rate limited")),
         "rate limited"),
        (FakeClaudeSandbox(exit_code=1), "failed"),
    ],
)
def test_failures_raise_with_cost(worktree, sandbox, msg):
    with pytest.raises(WorkerFailed, match=msg) as e:
        ClaudeCodeWorker("w1", "backend", sandbox).run(task(), worktree)
    assert e.value.cost_usd == 0.25


def test_unparseable_output_fails_without_cost(worktree):
    sb = FakeClaudeSandbox(line="Error: not logged in")
    with pytest.raises(WorkerFailed, match="no Claude Code result") as e:
        ClaudeCodeWorker("w1", "backend", sb).run(task(), worktree)
    assert e.value.cost_usd == 0.0


def test_parse_skips_non_result_json():
    out = '{"type": "system"}\n' + result_line(result="ok") + "\nstderr tail\n"
    assert parse_result_json(out)["result"] == "ok"


def test_usage_sums_every_model_and_rides_on_results_and_failures(worktree):
    model_usage = {"modelUsage": {
        "claude-sonnet-5-5": {"inputTokens": 10, "cacheReadInputTokens": 900,
                              "cacheCreationInputTokens": 50, "outputTokens": 300},
        "claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 7},
    }}
    expected = Usage(15, 900, 50, 307)
    assert usage_from_result(model_usage) == expected
    sb = FakeClaudeSandbox(effect=lambda wd: (wd / "shout.py").write_text("S = 1\n"),
                           line=result_line(**model_usage))
    assert ClaudeCodeWorker("w1", "backend", sb).run(task(), worktree).usage == expected
    with pytest.raises(WorkerFailed) as e:
        failing = FakeClaudeSandbox(line=result_line(is_error=True, **model_usage))
        ClaudeCodeWorker("w1", "backend", failing).run(task(), worktree)
    assert e.value.usage == expected


def test_usage_falls_back_to_top_level_usage():
    data = {"usage": {"input_tokens": 3, "cache_read_input_tokens": 4,
                      "cache_creation_input_tokens": 5, "output_tokens": 6}}
    assert usage_from_result(data) == Usage(3, 4, 5, 6)
    assert usage_from_result({}) == Usage()
