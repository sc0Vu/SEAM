"""SEAM entrypoint: add-goal | run | status | inspect.

The only place that prints or exits; everything below raises typed exceptions.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from coordinator.lead import Budget, Lead, load_plan
from engine.errors import SeamError
from engine.git import GitRepo
from engine.merge import MergeEngine
from engine.orchestrator import Orchestrator
from engine.states import TERMINAL, State
from engine.usage import format_usage, totals_by_role
from reviewer.claude_code import ClaudeCodeReviewer
from reviewer.gate import ReviewGate
from sandbox.docker import DEFAULT_IMAGE, CredentialProxyContainer, DockerSandbox
from sandbox.proxy import CREDENTIAL_KINDS
from store import TaskStore
from workers.claude_code import ClaudeCodeWorker

AUTH_ENV = tuple(CREDENTIAL_KINDS)  # the real one goes only to the credential proxy

_PROXY_PROBE = """python3 - <<'EOF'
import socket, time, urllib.request
for _ in range(50):
    try:
        urllib.request.urlopen('{health}', timeout=2).read(); print('proxy ok'); break
    except OSError:
        time.sleep(0.2)
try:
    socket.create_connection(('1.1.1.1', 443), 3); print('egress open')
except OSError:
    print('egress blocked')
EOF"""


class PreflightError(SeamError):
    """The environment can't run a team; raised before any tokens are spent."""


def _store(state_dir: Path) -> TaskStore:
    state_dir.mkdir(parents=True, exist_ok=True)
    return TaskStore(state_dir / "seam.sqlite")


def cmd_add_goal(args: argparse.Namespace) -> None:
    plan = load_plan(args.plan)
    store = _store(args.state_dir)
    created = Lead(store, args.goal or plan.goal, Budget(0)).submit_plan(plan)
    for key, t in created.items():
        print(f"task {t.id:>3}  {key:<20} {t.spec.type:<9} scope={list(t.spec.file_scope)}")


def select_credential() -> str:
    """The one credential env var the proxy will hold. Exactly one must be set, so
    which account pays is never ambiguous."""
    present = [k for k in AUTH_ENV if os.environ.get(k)]
    if not present:
        raise PreflightError(f"workers need one of {AUTH_ENV} in the environment "
                             "(e.g. `claude setup-token`, then export CLAUDE_CODE_OAUTH_TOKEN)")
    if len(present) > 1:
        raise PreflightError(f"both {present} are set; unset one so it is clear which "
                             "account the workers use")
    return present[0]


def preflight(args: argparse.Namespace, repo: GitRepo, sandbox: DockerSandbox) -> str:
    """Check everything a run needs before any tokens are spent; return the credential."""
    credential = select_credential()
    if shutil.which("claude") is None:
        raise PreflightError("the reviewer needs the `claude` CLI on PATH")
    if repo.checked_out_in(args.target):
        raise PreflightError(
            f"{args.target!r} is checked out in {repo.checked_out_in(args.target)}; SEAM "
            "moves that branch, so run it on a dedicated clone "
            f"(git clone {args.repo} <dir>; git -C <dir> checkout --detach)")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise PreflightError("docker daemon not reachable (colima start?)")
    if subprocess.run(["docker", "image", "inspect", args.image],
                      capture_output=True).returncode != 0:
        raise PreflightError(f"image {args.image!r} missing: docker build -t {args.image} sandbox/")
    probe = args.state_dir / "tmp"
    probe.mkdir(parents=True, exist_ok=True)
    (probe / ".seam-probe").write_text("ok")
    if not sandbox.run("test -f .seam-probe", probe, timeout=120).ok:
        raise PreflightError(f"{args.state_dir} is not visible inside containers "
                             "(colima shares only $HOME by default: use a state dir under it)")
    return credential


def check_proxy(proxy: CredentialProxyContainer, image: str, probe_dir: Path) -> None:
    """From the worker network: the proxy must answer and nothing else be reachable."""
    box = DockerSandbox(image, network=proxy.network)
    out = box.run(_PROXY_PROBE.format(health=proxy.health_url), probe_dir, timeout=120).output
    if "proxy ok" not in out:
        raise PreflightError(f"credential proxy not reachable from {proxy.network}: {out[-300:]}")
    if "egress blocked" not in out:
        raise PreflightError(f"{proxy.network} is not isolated: workers could reach the internet")


def cmd_run(args: argparse.Namespace) -> None:
    repo = GitRepo(args.repo)
    tests = DockerSandbox(args.image)
    credential = preflight(args, repo, tests)
    if args.plan:
        cmd_add_goal(args)
    store = _store(args.state_dir)
    scratch = args.state_dir / "tmp"
    with CredentialProxyContainer(credential, args.image) as proxy:
        check_proxy(proxy, args.image, scratch)
        _run_team(args, store, repo, tests, scratch, proxy)
    for line in proxy.refused():
        print(f"warning: {line}")


def _run_team(args, store, repo, tests, scratch, proxy) -> None:
    # Workers see only the proxy URL and a placeholder credential (doc §10).
    worker_box = DockerSandbox(args.image, network=proxy.network,
                               env_values=proxy.worker_env(), readonly=(".git",))
    types = sorted({t.spec.type for t in store.list() if t.state not in TERMINAL})
    workers = [
        ClaudeCodeWorker(f"{ty}-{i}", ty, worker_box, model=args.model,
                         max_budget_usd=args.run_budget_usd)
        for ty in types for i in range(1, args.workers + 1)
    ]
    if not workers:
        raise PreflightError("no unfinished tasks: pass --plan or run add-goal first")
    reviewer = ClaudeCodeReviewer(model=args.reviewer_model or args.model)
    lead = Lead(store, args.goal or "goal", Budget(args.budget_usd))
    orch = Orchestrator(
        store, lead, repo, workers,
        gate=ReviewGate(repo, tests, reviewer, target=args.target, scratch=scratch),
        merger=MergeEngine(store, repo, tests, suite_cmd=args.suite_cmd,
                           target=args.target, scratch=scratch),
        worktree_root=args.state_dir / "worktrees",
        target=args.target,
    )
    print(f"running {len(workers)} worker(s) on {args.repo} (budget ${args.budget_usd:.2f})")
    report = orch.run()
    _print_tasks(report.tasks)
    print(f"\nspent ${report.spent_usd:.2f}"
          + ("  (BUDGET EXHAUSTED: dispatch stopped)" if report.budget_exhausted else ""))
    _print_usage(store)


def _print_usage(store: TaskStore) -> None:
    for role, (n, cost, usage) in sorted(totals_by_role(store.runs()).items()):
        print(f"  {role:<8} {n:>2} run(s)  ${cost:.3f}  {format_usage(usage)}")


def _print_tasks(tasks) -> None:
    for t in tasks:
        note = t.review_notes.strip().splitlines()[0][:70] if t.review_notes.strip() else ""
        print(f"task {t.id:>3}  {t.state:<16} att={t.attempts}  {t.spec.description[:40]:<40}"
              f"  {note}")


def cmd_status(args: argparse.Namespace) -> None:
    tasks = _store(args.state_dir).list()
    counts = Counter(t.state for t in tasks)
    print("  ".join(f"{s}={n}" for s, n in sorted(counts.items())) or "no tasks")
    store = _store(args.state_dir)
    if store.runs():
        print("usage:")
        _print_usage(store)
    merging = [t.id for t in tasks if t.state is State.MERGING]
    queued = [t.id for t in tasks if t.state is State.QUEUED_FOR_MERGE]
    print(f"merge slot: {merging or 'free'}   queue: {queued}")
    _print_tasks(tasks)
    for t in tasks:
        if t.state is State.NEEDS_HUMAN:
            print(f"\n== ESCALATED task {t.id}: {t.spec.description}\n{t.review_notes}")


def cmd_inspect(args: argparse.Namespace) -> None:
    store = _store(args.state_dir)
    t = store.get(args.task)
    print(f"task {t.id}  [{t.state}]  attempts={t.attempts}  owner={t.owner}")
    print(f"description: {t.spec.description}\nspec_ref: {t.spec.spec_ref}")
    print(f"file_scope: {list(t.spec.file_scope)}  blocked_by: {list(t.spec.blocked_by)}")
    print(f"branch: {t.branch}  worktree: {t.worktree_path}")
    print(f"test_cmd: {t.spec.test_cmd}  merge: {t.base_sha} -> {t.merge_sha}")
    print("\n--- runs ---")
    for r in store.runs(t.id) or []:
        print(f"{r.role:<8} {r.agent_id:<12} {r.outcome:<13} ${r.cost_usd:.3f}  "
              f"{format_usage(r.usage)}")
    print(f"\n--- review notes ---\n{t.review_notes or '(none)'}")
    print(f"\n--- diff ---\n{t.diff or '(none)'}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seam", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--state-dir", type=lambda s: Path(s).expanduser().resolve(),
                        default=Path(".seam").resolve(), help="task store + worktrees")

    g = sub.add_parser("add-goal", help="load a plan's tasks into the store")
    common(g)
    g.add_argument("--plan", required=True)
    g.add_argument("--goal", help="goal label (defaults to the plan's)")
    g.set_defaults(func=cmd_add_goal)

    r = sub.add_parser("run", help="run a team until no more progress is possible")
    common(r)
    r.add_argument("--repo", required=True, type=lambda s: Path(s).expanduser().resolve())
    r.add_argument("--plan", help="load this plan first (else run existing tasks)")
    r.add_argument("--goal")
    r.add_argument("--target", default="main")
    r.add_argument("--workers", type=int, default=2, help="workers per task type")
    r.add_argument("--budget-usd", type=float, default=5.0, help="ceiling for the goal")
    r.add_argument("--run-budget-usd", type=float, default=1.0, help="ceiling per worker run")
    r.add_argument("--suite-cmd", default="python -m pytest -q", help="the FULL suite")
    r.add_argument("--image", default=DEFAULT_IMAGE)
    r.add_argument("--model")
    r.add_argument("--reviewer-model")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="task states, merge queue, escalations")
    common(s)
    s.set_defaults(func=cmd_status)

    i = sub.add_parser("inspect", help="one task's branch, diff, review notes")
    common(i)
    i.add_argument("--task", type=int, required=True)
    i.set_defaults(func=cmd_inspect)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.func(args)
    except SeamError as e:
        print(f"seam: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
