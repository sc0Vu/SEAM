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


def test_plan_refuses_an_existing_out_file_before_anything_else(tmp_path, capsys):
    repo = make_repo(tmp_path, {"a.py": ""})
    out = tmp_path / "plan.json"
    out.write_text("{}")
    assert cli.main(["plan", "--repo", str(repo), "--goal", "g", "--out", str(out),
                     "--state-dir", str(tmp_path / "s")]) == 1
    assert "exists" in capsys.readouterr().err


def _escalated_store(state):
    """A state dir with task 1 escalated after review rejections."""
    from engine.states import Event, State
    from store import TaskStore

    assert cli.main(["add-goal", "--state-dir", state, "--plan", str(EXAMPLES / "case1.json")]) == 0
    s = TaskStore(Path(state) / "seam.sqlite")
    s.claim("w1", "backend")
    s.transition(1, Event.START)
    while s.get(1).state is not State.NEEDS_HUMAN:
        s.transition(1, Event.SUBMIT)
        s.transition(1, Event.PICKUP)
        s.transition(1, Event.REQUEST_CHANGES, review_notes="still wrong")
    s.close()


def test_status_shows_the_rulings_that_apply(tmp_path, capsys):
    state = str(tmp_path / "s")
    _escalated_store(state)
    capsys.readouterr()
    assert cli.main(["status", "--state-dir", state]) == 0
    out = capsys.readouterr().out
    assert "ESCALATED task 1" in out
    assert "rework --task 1" in out and "cancel --task 1" in out
    assert "resolve --task 1" not in out  # it failed review: resolve does not apply


def test_rework_and_cancel_from_the_cli(tmp_path, capsys):
    state = str(tmp_path / "s")
    _escalated_store(state)
    assert cli.main(["resolve", "--state-dir", state, "--task", "1"]) == 1
    assert "no approving review" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # --notes is required for rework
        cli.main(["rework", "--state-dir", state, "--task", "1"])
    assert cli.main(["rework", "--state-dir", state, "--task", "1", "--notes", "hint"]) == 0
    assert "in_progress" in capsys.readouterr().out
    assert cli.main(["cancel", "--state-dir", state, "--task", "2"]) == 0
    assert "cancelled: task 2" in capsys.readouterr().out


def test_writers_are_refused_while_another_holds_the_state_dir(tmp_path, capsys):
    from store.lock import writer_lock

    state = tmp_path / "s"
    _escalated_store(str(state))
    with writer_lock(state, "run"):
        assert cli.main(["cancel", "--state-dir", str(state), "--task", "1"]) == 1
        assert "StoreBusy" in capsys.readouterr().err
        assert cli.main(["add-goal", "--state-dir", str(state), "--goal", "g2",
                         "--plan", str(EXAMPLES / "case2.json")]) == 1
        assert cli.main(["status", "--state-dir", str(state)]) == 0  # readers are fine
        assert "writing now: run, pid" in capsys.readouterr().out


def test_amend_from_the_cli_and_status_shows_the_link(tmp_path, capsys):
    state = str(tmp_path / "s")
    assert cli.main(["add-goal", "--state-dir", state, "--plan", str(EXAMPLES / "case1.json")]) == 0
    old = load_plan(EXAMPLES / "case1.json").goal
    capsys.readouterr()
    args = ["amend", "--state-dir", state, "--from-goal", old, "--goal", "v2",
            "--plan", str(EXAMPLES / "case2.json")]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert f"amends {old!r}" in out and "cancelled: task 1, task 2" in out
    assert cli.main(["status", "--state-dir", state]) == 0
    assert f"goal 'v2' amends {old!r}" in capsys.readouterr().out
    assert cli.main(args) == 1  # v2 is loaded now: a clean error, nothing changes


def test_rereview_from_the_cli_only_after_an_environment_failure(tmp_path, capsys):
    state = str(tmp_path / "s")
    _escalated_store(state)  # escalated by review rejections, not the environment
    assert cli.main(["rereview", "--state-dir", state, "--task", "1"]) == 1
    assert "nothing to re-review" in capsys.readouterr().err
