"""The MCP server (seam_merge/mcp_server.py): protocol, tools, and the limits fixed at launch.
No model calls: background jobs are started with a fake Popen."""

import io
import json
from pathlib import Path

import pytest

from seam_merge import mcp_server as M
from seam_merge.store.lock import writer_lock
from tests.test_cli import _escalated_store

EXAMPLES = Path(__file__).parent.parent / "examples" / "greeter"


@pytest.fixture
def cfg(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return M.Config(state_dir=tmp_path / "state", repos=(repo.resolve(),), max_budget_usd=3)


def call(cfg, _tool, **args):
    reply = M.handle(cfg, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": _tool, "arguments": args}})
    result = reply["result"]
    return result["content"][0]["text"], result["isError"]


class FakePopen:
    started: list[list[str]] = []

    def __init__(self, argv, **kw):
        FakePopen.started.append(argv)
        self.kw = kw

    def wait(self):
        return 0


@pytest.fixture
def fake_jobs(monkeypatch):
    """The `seam_merge.cli ...` part of each job started (after the `run_job` wrapper)."""
    FakePopen.started = []
    monkeypatch.setattr(M.subprocess, "Popen", FakePopen)

    class Started(list):
        def __getitem__(self, i):
            argv = FakePopen.started[i]
            assert argv[1:5] == ["-I", "-m", "seam_merge.mcp_server", M.JOB_FLAG]  # the wrapper
            return argv[argv.index(M.CLI_MODULE):]

        def __eq__(self, other):
            return FakePopen.started == other

        def __len__(self):
            return len(FakePopen.started)

    return Started()


# -- protocol ----------------------------------------------------------------------

def test_initialize_negotiates_the_protocol_version(cfg):
    for asked, got in (("2025-03-26", "2025-03-26"), ("1999-01-01", M.PROTOCOL_VERSIONS[0])):
        r = M.handle(cfg, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": asked}})["result"]
        assert r["protocolVersion"] == got
        assert r["capabilities"] == {"tools": {"listChanged": False}}
        assert r["serverInfo"]["name"] == "seam"


def test_notifications_get_no_reply_and_unknown_methods_an_error(cfg):
    assert M.handle(cfg, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    err = M.handle(cfg, {"jsonrpc": "2.0", "id": 2, "method": "resources/list"})["error"]
    assert err["code"] == -32601
    assert M.handle(cfg, {"id": 3, "method": "ping"})["error"]["code"] == -32600


def test_tools_are_listed_with_schemas_and_hints(cfg):
    tools = M.handle(cfg, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    by = {t["name"]: t for t in tools}
    assert set(by) == {"status", "inspect", "resolve", "rework", "rereview", "cancel", "amend",
                       "draft_plan",
                       "save_plan", "show_plan", "run", "job"}
    assert by["status"]["annotations"]["readOnlyHint"] is True
    assert by["cancel"]["annotations"]["destructiveHint"] is True
    assert "budget_usd" in by["run"]["inputSchema"]["required"]
    assert by["rework"]["inputSchema"]["required"] == ["task", "notes"]


def test_serve_speaks_newline_delimited_json_rpc(cfg):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n\nnot json\n")
    stdout = io.StringIO()
    M.serve(cfg, stdin, stdout)
    replies = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[2]["error"]["code"] == -32700


def test_a_bug_in_a_tool_is_an_error_result_not_a_crash(cfg, monkeypatch):
    monkeypatch.setattr(M._BY_NAME["status"], "handler", lambda c, a: 1 / 0)
    text, failed = call(cfg, "status")
    assert failed and "ZeroDivisionError" in text


# -- reading and ruling --------------------------------------------------------------

def test_status_inspect_and_rulings_go_through_the_cli(cfg):
    _escalated_store(str(cfg.state_dir))
    text, failed = call(cfg, "status")
    assert not failed and "ESCALATED task 1" in text
    assert not call(cfg, "inspect", task=1)[1]

    text, failed = call(cfg, "resolve", task=1)  # it failed review
    assert failed and "no approving review" in text
    text, failed = call(cfg, "rework", task=1, notes="try the parser")
    assert not failed and "in_progress" in text
    text, failed = call(cfg, "cancel", task=2, notes="not needed")
    assert not failed and "cancelled: task 2" in text


def test_bad_task_ids_are_refused(cfg):
    for bad in (0, -1, "1", True, None):
        assert call(cfg, "inspect", task=bad)[1]


def test_rulings_wait_for_a_running_team(cfg):
    _escalated_store(str(cfg.state_dir))
    with writer_lock(cfg.state_dir, "run"):
        text, failed = call(cfg, "cancel", task=1)
    assert failed and "StoreBusy" in text


# -- plans -------------------------------------------------------------------------

def test_save_and_show_plan_validate_like_hand_written_plans(cfg):
    plan = json.loads((EXAMPLES / "case1.json").read_text())
    text, failed = call(cfg, "save_plan", name="p1", plan=plan)
    assert not failed and "2 task(s)" in text
    assert json.loads(call(cfg, "show_plan", name="p1")[0]) == plan
    assert not call(cfg, "save_plan", name="p2", plan=json.dumps(plan))[1]  # string too

    text, failed = call(cfg, "save_plan", name="bad", plan={"goal": "g", "tasks": []})
    assert failed and "PlanError" in text
    text, failed = call(cfg, "show_plan", name="nope")
    assert failed and "['p1', 'p2']" in text


@pytest.mark.parametrize("name", ["../escape", "a/b", "", ".hidden", "x" * 65])
def test_plan_names_cannot_escape_the_plans_dir(cfg, name):
    plan = json.loads((EXAMPLES / "case1.json").read_text())
    assert call(cfg, "save_plan", name=name, plan=plan)[1]
    assert not (cfg.state_dir / "escape.json").exists()


# -- spending: repos, budgets, and background jobs ---------------------------------

def test_spending_tools_need_an_allowed_repo_and_a_capped_budget(cfg, fake_jobs):
    repo = str(cfg.repos[0])
    cases = [
        ({"repo": "/etc", "budget_usd": 1}, "not allowed"),
        ({"repo": repo}, "budget_usd is required"),
        ({"repo": repo, "budget_usd": 0}, "budget_usd is required"),
        ({"repo": repo, "budget_usd": True}, "budget_usd is required"),
        ({"repo": repo, "budget_usd": 3.5}, "exceeds this server's cap"),
        ({"repo": repo, "budget_usd": 1, "workers": 50}, "workers must be"),
        ({"repo": repo, "budget_usd": 1, "target": "-x"}, "plain branch"),
        ({"repo": repo, "budget_usd": 1, "plan": "missing"}, "no saved plan"),
    ]
    for args, why in cases:
        text, failed = call(cfg, "run", **args)
        assert failed and why in text, (args, text)
    text, failed = call(cfg, "draft_plan", repo=repo, goal="g", name="n", budget_usd=9)
    assert failed and "cap" in text
    assert fake_jobs == []  # nothing was started


def test_run_starts_a_background_cli_run(cfg, fake_jobs):
    call(cfg, "save_plan", name="p1", plan=json.loads((EXAMPLES / "case1.json").read_text()))
    text, failed = call(cfg, "run", repo=str(cfg.repos[0]), budget_usd=2, plan="p1",
                        workers=1, suite_cmd="pytest -q")
    assert not failed and "started job run-" in text
    argv = fake_jobs[0]
    assert argv[0] == M.CLI_MODULE and argv[1] == "run"
    flags = dict(zip(argv[2::2], argv[3::2], strict=False))
    assert flags["--budget-usd"] == "2.0" and flags["--repo"] == str(cfg.repos[0])
    assert flags["--plan"] == str(cfg.plans / "p1.json")
    assert flags["--state-dir"] == str(cfg.state_dir) and flags["--workers"] == "1"


def test_run_is_refused_while_another_writer_holds_the_state_dir(cfg, fake_jobs):
    with writer_lock(cfg.state_dir, "run"):
        text, failed = call(cfg, "run", repo=str(cfg.repos[0]), budget_usd=1)
    assert failed and "busy" in text and fake_jobs == []


def test_draft_plan_starts_the_planner_into_the_plans_dir(cfg, fake_jobs):
    text, failed = call(cfg, "draft_plan", repo=str(cfg.repos[0]), goal="add x",
                        name="d1", budget_usd=0.5)
    assert not failed and "show_plan" in text
    flags = dict(zip(fake_jobs[0][2::2], fake_jobs[0][3::2], strict=False))
    assert fake_jobs[0][1] == "plan" and flags["--out"] == str(cfg.plans / "d1.json")
    assert flags["--goal"] == "add x" and flags["--budget-usd"] == "0.5"


def test_job_reports_progress_and_exit(cfg):
    assert call(cfg, "job")[0] == "jobs: none"
    cfg.jobs.mkdir(parents=True)
    (cfg.jobs / "run-1.log").write_text("running 2 worker(s)\n")
    assert "running" in call(cfg, "job", job="run-1")[0]
    (cfg.jobs / "run-1.log").write_text("spent $0.10\n\n[seam-mcp] exit 0\n")
    assert "finished, exit 0" in call(cfg, "job", job="run-1")[0]
    assert call(cfg, "job", job="../x")[1]


def test_a_real_background_job_logs_its_exit(cfg):
    """No fake: a real (instant) `seam status` process, to check the log plumbing."""
    import time

    job = M._start_job(cfg, "status", ["status", "--state-dir", str(cfg.state_dir)])
    deadline = time.time() + 30
    while time.time() < deadline:
        text = call(cfg, "job", job=job)[0]
        if "finished" in text:
            break
        time.sleep(0.1)
    assert "finished, exit 0" in text and "no tasks" in text


def test_amend_tool_uses_a_saved_plan(cfg):
    case1 = json.loads((EXAMPLES / "case1.json").read_text())
    case2 = json.loads((EXAMPLES / "case2.json").read_text())
    call(cfg, "save_plan", name="v1", plan=case1)
    call(cfg, "save_plan", name="v2", plan={**case2, "goal": "v2"})
    assert M.cli.main(["add-goal", "--state-dir", str(cfg.state_dir),
                       "--plan", str(cfg.plans / "v1.json")]) == 0
    text, failed = call(cfg, "amend", from_goal=case1["goal"], plan="missing")
    assert failed and "no saved plan" in text
    text, failed = call(cfg, "amend", from_goal=case1["goal"], plan="v2")
    assert not failed and "amends" in text and "cancelled: task 1, task 2" in text


def test_a_job_logs_its_exit_even_after_the_server_is_gone(cfg):
    """The exit line is written by the job's own process, not by the server: here
    the job is started by a process that exits at once (as a server does when a
    client starts one per call), and the log still records how the job ended."""
    import subprocess
    import sys
    import time

    starter = (f"from seam_merge import mcp_server as M; import pathlib; "
               f"cfg = M.Config(pathlib.Path({str(cfg.state_dir)!r}), (), 1.0); "
               f"print(M._start_job(cfg, 'status', "
               f"['status', '--state-dir', {str(cfg.state_dir)!r}]))")
    job = subprocess.run([sys.executable, "-c", starter], cwd=Path(M.__file__).parent,
                         capture_output=True, text=True, check=True).stdout.strip()
    log = cfg.jobs / f"{job}.log"
    deadline = time.time() + 30
    while time.time() < deadline and "[seam-mcp] exit" not in (
            log.read_text() if log.exists() else ""):
        time.sleep(0.1)
    assert log.read_text().rstrip().endswith("[seam-mcp] exit 0")


def test_run_job_appends_output_and_the_exit_code(tmp_path):
    log = tmp_path / "j.log"
    log.write_text("earlier\n")
    assert M.run_job(str(log), ["-c", "print('hi'); raise SystemExit(3)"]) == 3
    assert log.read_text() == "earlier\nhi\n\n[seam-mcp] exit 3\n"
