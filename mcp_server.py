"""SEAM as an MCP server (stdio): drive SEAM from Claude Code, Claude Desktop, or any
MCP client, including remotely through the client's own remote access.

    python mcp_server.py --state-dir ~/seam-demo/x-state --repo ~/seam-demo/x \
        [--max-budget-usd 5]

What a client can do is fixed when the server starts, not by the client: one state
dir, the repos it may plan or run on (`--repo`, repeatable), and the most a single run
or plan may spend (`--max-budget-usd`). A client, or a prompt injected into one,
cannot point SEAM at other paths or start an uncapped run.

Tools: status, inspect, the human rulings (resolve, rework, rereview, cancel), plans (draft,
save, show), amend (change a goal's direction), run, and job (a background job's
log). `run` and `draft_plan` start `cli.py` as a background process and return at
once; `run` takes the state dir's writer lock itself, so this server never writes
task state concurrently with it (the single-writer condition for sqlite). Everything
else goes through `cli.main`, so it gets the same checks, locking, and messages as
the command line.

Protocol: MCP over stdio, newline-delimited JSON-RPC 2.0, tools only. Stdlib only, as
SEAM has no runtime dependencies. stdout carries protocol messages ONLY; logs go to
stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import cli
from coordinator.lead import plan_from_dict
from engine.errors import SeamError
from store.lock import active_writer

SERVER_NAME = "seam"
SERVER_VERSION = "0.0.1"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
CLI = Path(__file__).with_name("cli.py")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ToolError(SeamError):
    """A tool call that cannot be carried out; reported to the client as an error
    result (not a protocol error), so the model sees the reason."""


@dataclass
class Config:
    """What the server allows, fixed at launch."""

    state_dir: Path
    repos: tuple[Path, ...]
    max_budget_usd: float = 5.0

    @property
    def plans(self) -> Path:
        return self.state_dir / "plans"

    @property
    def jobs(self) -> Path:
        return self.state_dir / "jobs"


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[Config, dict], str]
    read_only: bool = False
    destructive: bool = False
    extra: dict = field(default_factory=dict)

    def listing(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {"type": "object", "additionalProperties": False, **self.schema},
            "annotations": {"readOnlyHint": self.read_only,
                            "destructiveHint": self.destructive,
                            "openWorldHint": False},
        }


# -- tool handlers ---------------------------------------------------------------

def _cli(cfg: Config, *argv: str) -> str:
    """Run one `cli.py` subcommand in-process, returning its output; a failure
    becomes a ToolError carrying the CLI's own message."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([argv[0], "--state-dir", str(cfg.state_dir), *argv[1:]])
        except SystemExit as e:  # argparse rejected the arguments
            code = e.code if isinstance(e.code, int) else 2
    if code != 0:
        raise ToolError((err.getvalue() or out.getvalue()).strip() or f"exit {code}")
    return out.getvalue().strip()


def _task(args: dict) -> str:
    task = args.get("task")
    if not isinstance(task, int) or isinstance(task, bool) or task < 1:
        raise ToolError("task must be a positive integer task id")
    return str(task)


def _notes(args: dict) -> str:
    notes = args.get("notes", "")
    if not isinstance(notes, str):
        raise ToolError("notes must be a string")
    return notes


def status(cfg: Config, args: dict) -> str:
    return _cli(cfg, "status")


def inspect(cfg: Config, args: dict) -> str:
    return _cli(cfg, "inspect", "--task", _task(args))


def resolve(cfg: Config, args: dict) -> str:
    return _cli(cfg, "resolve", "--task", _task(args), "--notes", _notes(args))


def rework(cfg: Config, args: dict) -> str:
    return _cli(cfg, "rework", "--task", _task(args), "--notes", _notes(args))


def rereview(cfg: Config, args: dict) -> str:
    return _cli(cfg, "rereview", "--task", _task(args), "--notes", _notes(args))


def cancel(cfg: Config, args: dict) -> str:
    return _cli(cfg, "cancel", "--task", _task(args), "--notes", _notes(args))


def _name(args: dict, key: str = "name") -> str:
    name = args.get(key)
    if not isinstance(name, str) or not _NAME.match(name):
        raise ToolError(f"{key} must be 1-64 letters, digits, '.', '_' or '-'")
    return name


def _plan_path(cfg: Config, name: str) -> Path:
    return cfg.plans / f"{name}.json"


def show_plan(cfg: Config, args: dict) -> str:
    path = _plan_path(cfg, _name(args))
    if not path.exists():
        have = sorted(p.stem for p in cfg.plans.glob("*.json")) if cfg.plans.exists() else []
        raise ToolError(f"no plan {path.stem!r}; saved plans: {have}")
    return path.read_text()


def save_plan(cfg: Config, args: dict) -> str:
    """Validate a plan with the same rules as a hand-written one, then save it."""
    name, plan = _name(args), args.get("plan")
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError as e:
            raise ToolError(f"plan is not valid JSON: {e}") from None
    checked = plan_from_dict(plan)  # PlanError (a SeamError) if invalid
    cfg.plans.mkdir(parents=True, exist_ok=True)
    _plan_path(cfg, name).write_text(json.dumps(plan, indent=2) + "\n")
    return f"saved plan {name!r}: {len(checked.tasks)} task(s) for goal {checked.goal!r}"


def amend(cfg: Config, args: dict) -> str:
    """Replace a goal with a saved plan; same rules as `cli.py amend`."""
    old, plan = args.get("from_goal"), _name(args, "plan")
    if not isinstance(old, str) or not old:
        raise ToolError("from_goal is required")
    path = _plan_path(cfg, plan)
    if not path.exists():
        raise ToolError(f"no saved plan {plan!r}")
    argv = ["amend", "--from-goal", old, "--plan", str(path)]
    if args.get("cancel_in_flight") is True:
        argv.append("--cancel-in-flight")
    return _cli(cfg, *argv)


def _repo(cfg: Config, args: dict) -> Path:
    raw = args.get("repo")
    if not isinstance(raw, str):
        raise ToolError("repo is required")
    repo = Path(raw).expanduser().resolve()
    if repo not in cfg.repos:
        raise ToolError(f"repo {repo} is not allowed; this server allows "
                        f"{[str(r) for r in cfg.repos]} (set with --repo at launch)")
    return repo


def _budget(cfg: Config, args: dict) -> float:
    budget = args.get("budget_usd")
    if not isinstance(budget, int | float) or isinstance(budget, bool) or budget <= 0:
        raise ToolError("budget_usd is required: a positive dollar ceiling")
    if budget > cfg.max_budget_usd:
        raise ToolError(f"budget_usd {budget} exceeds this server's cap of "
                        f"${cfg.max_budget_usd:.2f} (--max-budget-usd at launch)")
    return float(budget)


def _start_job(cfg: Config, kind: str, argv: list[str]) -> str:
    """Start `cli.py <argv>` detached, logging to jobs/<id>.log; return the job id.

    The job runs under `run_job` in its own process and session, which appends the
    exit code to the log itself: the server may exit or restart mid-run (a client
    that starts one server per call, a Desktop restart) and `job` still sees it end.
    """
    cfg.jobs.mkdir(parents=True, exist_ok=True)
    job = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}"
    log_path = cfg.jobs / f"{job}.log"
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), JOB_FLAG,
                      str(log_path), str(CLI), *argv],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, cwd=str(CLI.parent))
    return job


JOB_FLAG = "--run-job"


def run_job(log_path: str, argv: list[str]) -> int:
    """Run `python <argv>` with output appended to `log_path`, then append
    `[seam-mcp] exit <code>`: the line `job` reads to report a finished job."""
    with open(log_path, "a") as log:
        code = subprocess.call([sys.executable, *argv], stdin=subprocess.DEVNULL,
                               stdout=log, stderr=subprocess.STDOUT, cwd=str(CLI.parent))
        log.write(f"\n[seam-mcp] exit {code}\n")
    return code


def draft_plan(cfg: Config, args: dict) -> str:
    repo, name, budget = _repo(cfg, args), _name(args), _budget(cfg, args)
    goal = args.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ToolError("goal is required: the goal in prose")
    out = _plan_path(cfg, name)
    if out.exists():
        raise ToolError(f"plan {name!r} exists; pick another name")
    cfg.plans.mkdir(parents=True, exist_ok=True)
    argv = ["plan", "--state-dir", str(cfg.state_dir), "--repo", str(repo),
            "--goal", goal, "--out", str(out), "--budget-usd", str(budget),
            "--target", _target(args)]
    job = _start_job(cfg, "plan", argv)
    return (f"started job {job}: planning (cap ${budget:.2f}). Poll `job` for progress; "
            f"when done, read it with show_plan name={name!r}, then `run` it.")


def _target(args: dict) -> str:
    target = args.get("target", "main")
    if not isinstance(target, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", target):
        raise ToolError("target must be a plain branch name")
    return target


def run(cfg: Config, args: dict) -> str:
    repo, budget = _repo(cfg, args), _budget(cfg, args)
    writer = active_writer(cfg.state_dir)
    if writer:
        raise ToolError(f"the state dir is busy ({writer}); poll `status` until it is free")
    argv = ["run", "--state-dir", str(cfg.state_dir), "--repo", str(repo),
            "--budget-usd", str(budget), "--target", _target(args)]
    if "plan" in args:
        argv += ["--plan", str(_plan_path(cfg, _name(args, "plan")))]
        if not Path(argv[-1]).exists():
            raise ToolError(f"no saved plan {args['plan']!r}")
    workers = args.get("workers", 2)
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ToolError("workers must be 1-8")
    argv += ["--workers", str(workers)]
    suite = args.get("suite_cmd")
    if suite is not None:
        if not isinstance(suite, str) or not suite.strip():
            raise ToolError("suite_cmd must be a non-empty command")
        argv += ["--suite-cmd", suite]  # runs in the sandbox, like every test run
    job = _start_job(cfg, "run", argv)
    return (f"started job {job}: team run (cap ${budget:.2f}). Poll `status` for task "
            "states and `job` for the run's log.")


def job(cfg: Config, args: dict) -> str:
    if "job" not in args:
        logs = sorted(cfg.jobs.glob("*.log")) if cfg.jobs.exists() else []
        return "jobs: " + (", ".join(p.stem for p in logs[-20:]) or "none")
    path = cfg.jobs / f"{_name(args, 'job')}.log"
    if not path.exists():
        raise ToolError(f"no job {path.stem!r}")
    text = path.read_text(errors="replace")
    done = re.search(r"\[seam-mcp\] exit (-?\d+)\s*$", text)
    state = f"finished, exit {done.group(1)}" if done else "running (or the server restarted)"
    return f"job {path.stem}: {state}\n\n" + text[-6000:]


TASK = {"task": {"type": "integer", "minimum": 1, "description": "task id"}}
NOTES = {"notes": {"type": "string", "description": "your reasoning (recorded)"}}
BUDGET = {"budget_usd": {"type": "number", "exclusiveMinimum": 0,
                         "description": "dollar ceiling (required; capped by the server)"}}
REPO = {"repo": {"type": "string", "description": "path of an allowed repo clone"}}
TARGET = {"target": {"type": "string", "description": "branch SEAM merges into",
                     "default": "main"}}

TOOLS = [
    Tool("status", "Task states, merge queue, cost, escalations, and the rulings that "
         "apply to each escalated task.", {"properties": {}}, status, read_only=True),
    Tool("inspect", "One task: branch, diff, every agent run with cost and notes.",
         {"properties": TASK, "required": ["task"]}, inspect, read_only=True),
    Tool("resolve", "Requeue an escalated task whose branch a review approved (it "
         "escalated at merge time). The full test suite still runs at merge. Refused "
         "for a branch that failed review: use rework or cancel.",
         {"properties": {**TASK, **NOTES}, "required": ["task"]}, resolve),
    Tool("rework", "Send an escalated task back to a worker with guidance; its retry "
         "budget restarts. The next run picks it up.",
         {"properties": {**TASK, "notes": {"type": "string", "minLength": 1,
                                           "description": "guidance for the worker"}},
          "required": ["task", "notes"]}, rework),
    Tool("rereview", "Review an escalated task's unchanged branch again, after the "
         "sandbox environment its tests could not run in has been fixed. Only for "
         "tasks escalated by such an environment failure.",
         {"properties": {**TASK, **NOTES}, "required": ["task"]}, rereview),
    Tool("cancel", "Cancel a task and every task that depends on it.",
         {"properties": {**TASK, **NOTES}, "required": ["task"]}, cancel, destructive=True),
    Tool("draft_plan", "Start the LLM planner on a goal (background job; spends up to "
         "budget_usd). The plan is saved under `name` for review before any run.",
         {"properties": {**REPO, "goal": {"type": "string"},
                         "name": {"type": "string", "description": "plan name"},
                         **BUDGET, **TARGET},
          "required": ["repo", "goal", "name", "budget_usd"]}, draft_plan),
    Tool("amend", "Change direction: replace goal `from_goal` with a saved plan. "
         "Cancels the old goal's unstarted tasks (and, with cancel_in_flight, its started "
         "ones); merged work stays. The old goal's spend counts toward the new budget.",
         {"properties": {"from_goal": {"type": "string"},
                         "plan": {"type": "string", "description": "saved plan name"},
                         "cancel_in_flight": {"type": "boolean", "default": False}},
          "required": ["from_goal", "plan"]}, amend, destructive=True),
    Tool("save_plan", "Validate and save a plan (JSON object with goal and tasks), "
         "e.g. an edited draft.",
         {"properties": {"name": {"type": "string"},
                         "plan": {"type": ["object", "string"]}},
          "required": ["name", "plan"]}, save_plan),
    Tool("show_plan", "Read a saved plan.", {"properties": {"name": {"type": "string"}},
                                              "required": ["name"]},
         show_plan, read_only=True),
    Tool("run", "Start a team run (background job; spends up to budget_usd and runs "
         "agents in the sandbox). With `plan`, loads that saved plan first; without, "
         "resumes the unfinished tasks.",
         {"properties": {**REPO, **BUDGET, **TARGET,
                         "plan": {"type": "string", "description": "saved plan name"},
                         "workers": {"type": "integer", "minimum": 1, "maximum": 8},
                         "suite_cmd": {"type": "string",
                                       "description": "the FULL test suite command"}},
          "required": ["repo", "budget_usd"]}, run),
    Tool("job", "A background job's status and log tail; without `job`, list jobs.",
         {"properties": {"job": {"type": "string"}}}, job, read_only=True),
]
_BY_NAME = {t.name: t for t in TOOLS}


# -- protocol --------------------------------------------------------------------

def handle(cfg: Config, msg: dict) -> dict | None:
    """Answer one JSON-RPC message; None for notifications (no reply)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request")
    mid, method = msg.get("id"), msg.get("method")
    if "id" not in msg:
        return None  # a notification, e.g. notifications/initialized
    params = msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(mid, {"protocolVersion": version,
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                             "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [t.listing() for t in TOOLS]})
    if method == "tools/call":
        tool = _BY_NAME.get(params.get("name"))
        if tool is None:
            return _error(mid, -32602, f"unknown tool {params.get('name')!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(mid, -32602, "arguments must be an object")
        try:
            text, failed = tool.handler(cfg, args), False
        except SeamError as e:
            text, failed = f"{type(e).__name__}: {e}", True
        except Exception as e:  # a bug must not kill the server; report it
            text, failed = f"internal error: {type(e).__name__}: {e}", True
        return _result(mid, {"content": [{"type": "text", "text": text or "(no output)"}],
                             "isError": failed})
    return _error(mid, -32601, f"method not found: {method}")


INSTRUCTIONS = (
    "SEAM runs a team of coding agents on one repo and merges their work only after an "
    "independent review and the full test suite. Use `status` first. Escalated tasks "
    "(needs_human) wait for a human ruling: confirm with the user before resolve, "
    "rework or cancel, and before draft_plan or run, which spend money.")


def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(cfg: Config, stdin=sys.stdin, stdout=sys.stdout) -> None:
    """Read JSON-RPC messages line by line until stdin closes."""
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = handle(cfg, msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == [JOB_FLAG]:  # internal: a background job started by `_start_job`
        return run_job(argv[1], argv[2:])
    p = argparse.ArgumentParser(prog="seam-mcp", description=__doc__.splitlines()[0])
    p.add_argument("--state-dir", required=True,
                   type=lambda s: Path(s).expanduser().resolve())
    p.add_argument("--repo", action="append", default=[],
                   type=lambda s: Path(s).expanduser().resolve(),
                   help="a repo clone the client may plan or run on (repeatable)")
    p.add_argument("--max-budget-usd", type=float, default=5.0,
                   help="the most one run or plan may spend")
    a = p.parse_args(argv)
    cfg = Config(a.state_dir, tuple(a.repo), a.max_budget_usd)
    print(f"seam-mcp: state dir {cfg.state_dir}, repos {[str(r) for r in cfg.repos]}, "
          f"cap ${cfg.max_budget_usd:.2f}", file=sys.stderr)
    serve(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
