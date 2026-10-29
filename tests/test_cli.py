from pathlib import Path

import pytest

import cli
from coordinator.lead import load_plan
from engine.scope import scopes_overlap
from tests.gitfixture import git, make_repo

EXAMPLES = Path(__file__).parent.parent / "examples" / "greeter"


@pytest.mark.parametrize("plan", ["case1.json", "case2.json"])
def test_example_plans_are_valid_and_disjoint(plan):
    a, b = load_plan(EXAMPLES / plan).tasks
    # Disjoint scopes are what let both run in parallel (case 2's conflict is semantic).
    assert not scopes_overlap(a["file_scope"], b["file_scope"])


def test_add_goal_then_status_and_inspect(tmp_path, capsys):
    state = str(tmp_path / "state")
    assert cli.main(["add-goal", "--state-dir", state, "--plan", str(EXAMPLES / "case1.json")]) == 0
    assert "shout" in capsys.readouterr().out

    assert cli.main(["status", "--state-dir", state]) == 0
    out = capsys.readouterr().out
    assert "open=2" in out and "merge slot: free" in out

    assert cli.main(["inspect", "--state-dir", state, "--task", "1"]) == 0
    assert "shout.py" in capsys.readouterr().out


def test_inspect_unknown_task_is_a_clean_error(tmp_path, capsys):
    assert cli.main(["inspect", "--state-dir", str(tmp_path), "--task", "99"]) == 1
    assert "TaskNotFound" in capsys.readouterr().err


def test_run_refuses_without_auth_before_spending(tmp_path, monkeypatch, capsys):
    for k in cli.AUTH_ENV:
        monkeypatch.delenv(k, raising=False)
    repo = make_repo(tmp_path, {"a.py": ""})
    assert cli.main(["run", "--repo", str(repo), "--state-dir", str(tmp_path / "s")]) == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN" in capsys.readouterr().err


def test_run_refuses_checked_out_target(tmp_path, monkeypatch, capsys):
    for k in cli.AUTH_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-used")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/true")
    repo = make_repo(tmp_path, {"a.py": ""})
    git(repo, "checkout", "-q", "main")
    assert cli.main(["run", "--repo", str(repo), "--state-dir", str(tmp_path / "s")]) == 1
    assert "dedicated clone" in capsys.readouterr().err


def test_add_goal_twice_is_a_clean_error(tmp_path, capsys):
    args = ["add-goal", "--state-dir", str(tmp_path), "--plan", str(EXAMPLES / "case1.json")]
    assert cli.main(args) == 0
    assert cli.main(args) == 1
    assert "GoalAlreadyLoaded" in capsys.readouterr().err


def test_exactly_one_credential_must_be_set(monkeypatch):
    for k in cli.AUTH_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    assert cli.select_credential() == "CLAUDE_CODE_OAUTH_TOKEN"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    with pytest.raises(cli.PreflightError, match="unset one"):
        cli.select_credential()
