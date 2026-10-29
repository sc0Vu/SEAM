"""Claude Code headless as a worker runtime (`claude -p`), run inside the sandbox.

Everything Claude Code-specific lives here (invariant 8). The agent runs in a
container with only its worktree mounted (invariant 10); it edits files, and SEAM —
not the agent — stages and commits them.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from engine.git import GitRepo
from engine.task import Task
from engine.usage import Usage
from sandbox.base import Sandbox
from workers.base import WorkerFailed, WorkerResult

SCOPE_REQUEST_FILE = ".seam-scope-request"

_PROMPT = """\
You are a SEAM worker. Implement exactly ONE task in this repository (the current
directory). Other agents are working on other files of the same repository in
parallel, so the rules below are hard limits, not suggestions.

TASK: {description}
SPEC / CONTRACT: {spec}

YOU MAY ONLY CREATE OR EDIT FILES MATCHING: {scope}

Rules:
- Never edit a file outside that scope. If the task truly cannot be done without
  editing other files, do NOT edit them: write the globs you need, one per line, to
  the file `{request_file}` and stop immediately. A lead will decide.
- Never change a shared interface (a signature other modules use) unless the task
  explicitly says to.
- Add or update tests for your change, inside your scope. The task's tests run with:
  {test_cmd}
- Do not run git and do not commit. Leave your changes in the working tree.
{feedback}When done, reply with a short summary of what you changed.
"""

_FEEDBACK = """\
- A previous attempt is already in the working tree and was REJECTED. Fix it.
  Reviewer feedback:
  {notes}
"""


def parse_result_json(output: str) -> dict:
    """Find Claude Code's `--output-format json` result object in mixed output."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    raise ValueError("no Claude Code result object in output")


def usage_from_result(data: dict) -> Usage:
    """Tokens from a Claude Code result object. `modelUsage` sums every model the run
    used (including any helper model); fall back to the top-level `usage`."""
    per_model = data.get("modelUsage")
    if isinstance(per_model, dict) and per_model:
        total = Usage()
        for m in per_model.values():
            total += Usage(int(m.get("inputTokens") or 0),
                           int(m.get("cacheReadInputTokens") or 0),
                           int(m.get("cacheCreationInputTokens") or 0),
                           int(m.get("outputTokens") or 0))
        return total
    u = data.get("usage") or {}
    return Usage(int(u.get("input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0),
                 int(u.get("cache_creation_input_tokens") or 0), int(u.get("output_tokens") or 0))


class ClaudeCodeWorker:
    """`Worker` that runs Claude Code headless in a sandbox container.

    `sandbox` must allow network and pass the API key by name (see `DockerSandbox`
    `network` / `env_names`), and should overlay the worktree's `.git` read-only.
    """

    def __init__(
        self,
        id: str,
        type: str,
        sandbox: Sandbox,
        model: str | None = None,
        max_budget_usd: float = 1.0,
        timeout: float = 1800,
        claude: str = "claude",
    ) -> None:
        self.id = id
        self.type = type
        self.sandbox = sandbox
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.timeout = timeout
        self.claude = claude

    def prompt(self, task: Task) -> str:
        notes = task.review_notes.strip()
        return _PROMPT.format(
            description=task.spec.description,
            spec=task.spec.spec_ref or "(none beyond the task)",
            scope=", ".join(task.spec.file_scope),
            request_file=SCOPE_REQUEST_FILE,
            test_cmd=task.spec.test_cmd or "(none given)",
            feedback=_FEEDBACK.format(notes=notes.replace("\n", "\n  ")) if notes else "",
        )

    def command(self, task: Task) -> str:
        argv = [
            self.claude, "-p", self.prompt(task),
            "--output-format", "json",
            "--dangerously-skip-permissions",  # safe only because we are in the sandbox
            "--no-session-persistence",
            "--max-budget-usd", str(self.max_budget_usd),
        ]
        if self.model:
            argv += ["--model", self.model]
        return shlex.join(argv)

    def run(self, task: Task, worktree: Path) -> WorkerResult:
        worktree = Path(worktree)
        result = self.sandbox.run(self.command(task), worktree, self.timeout)
        try:
            data = parse_result_json(result.output)
        except ValueError as e:
            raise WorkerFailed(f"{e}; exit {result.exit_code}: {result.output[-1000:]}") from e
        cost = float(data.get("total_cost_usd") or 0.0)
        usage = usage_from_result(data)
        if data.get("is_error") or not result.ok:
            raise WorkerFailed(f"claude run failed: {data.get('result')!r}", cost, usage)

        request = worktree / SCOPE_REQUEST_FILE
        if request.exists():
            globs = tuple(g.strip() for g in request.read_text().splitlines() if g.strip())
            request.unlink()
            if globs:
                return WorkerResult(scope_request=globs, cost_usd=cost, usage=usage)

        diff = GitRepo(worktree).stage_all()
        if not diff:
            raise WorkerFailed("claude run changed nothing", cost, usage)
        return WorkerResult(diff=diff, cost_usd=cost, usage=usage)
