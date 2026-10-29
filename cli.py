"""SEAM entrypoint: plan | add-goal | run | status | inspect.

The only place that prints or exits; everything below raises typed exceptions.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from coordinator.lead import Budget, Lead, load_plan
from engine.errors import SeamError
from engine.git import GitRepo
from engine.merge import MergeEngine
from engine.orchestrator import Orchestrator
from engine.scope import scopes_overlap
from engine.states import TERMINAL, State
from engine.usage import format_usage, totals_by_role
from planners.claude_code import ClaudeCodePlanner, plan_to_json
from reviewer.claude_code import ClaudeCodeReviewer
from reviewer.gate import DEFAULT_PROBE_CMD, ReviewGate
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
    preflight_sandbox(args, sandbox)
    return credential


def preflight_sandbox(args: argparse.Namespace, sandbox: DockerSandbox) -> None:
    """Docker is up, the image exists, and the state dir is visible in containers."""
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


def check_proxy(proxy: CredentialProxyContainer, image: str, probe_dir: Path) -> None:
    """From the worker network: the proxy must answer and nothing else be reachable."""
    box = DockerSandbox(image, network=proxy.network)
    out = box.run(_PROXY_PROBE.format(health=proxy.health_url), probe_dir, timeout=120).output
    if "proxy ok" not in out:
        raise PreflightError(f"credential proxy not reachable from {proxy.network}: {out[-300:]}")
    if "egress blocked" not in out:
        raise PreflightError(f"{proxy.network} is not isolated: workers could reach the internet")


def cmd_plan(args: argparse.Namespace) -> None:
    """Draft a plan for a goal with the LLM planner (doc §12) and write it for review.
    It only reads: the target is exported (no `.git`) and nothing in the repo moves."""
    repo = GitRepo(args.repo)
    out = Path(args.out)
    if out.exists():
        raise PreflightError(f"{out} exists; pick another --out")
    credential = select_credential()
    preflight_sandbox(args, DockerSandbox(args.image))
    scratch = args.state_dir / "tmp"
    with tempfile.TemporaryDirectory(prefix="seam-plan-", dir=scratch) as d:
        repo.export(repo.rev_parse(args.target), Path(d))
        with CredentialProxyContainer(credential, args.image) as proxy:
            check_proxy(proxy, args.image, scratch)
            box = DockerSandbox(args.image, network=proxy.network,
                                env_values=proxy.worker_env())
            planner = ClaudeCodePlanner(box, model=args.model, max_budget_usd=args.budget_usd)
            print(f"planning on {args.repo}@{args.target} (cap ${args.budget_usd:.2f})")
            draft = planner.plan(args.goal, Path(d))
    out.write_text(plan_to_json(draft, args.goal))
    by_key = {t["key"]: t for t in draft.plan.tasks}
    for t in draft.plan.tasks:
        deps = f"  after {t.get('blocked_by')}" if t.get("blocked_by") else ""
        print(f"  {t['key']:<24} scope={t['file_scope']}{deps}")
    overlaps = [(a, b) for i, a in enumerate(by_key) for b in list(by_key)[i + 1:]
                if scopes_overlap(by_key[a]["file_scope"], by_key[b]["file_scope"])]
    if overlaps:
        print(f"  overlapping scopes (will run one at a time): {overlaps}")
    print(f"\nplanner notes: {draft.notes}")
    usage = format_usage(draft.usage) if draft.usage else "n/a"
    print(f"\nwrote {out}  (${draft.cost_usd:.2f}; {usage})")
    print(f"review it, then: cli.py run --repo {args.repo} --plan {out} ...")


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
    reviewer = ClaudeCodeReviewer(model=args.reviewer_model or args.model,
                                  probes=args.review_probes)
    lead = Lead(store, args.goal or "goal", Budget(args.budget_usd))
    orch = Orchestrator(
        store, lead, repo, workers,
        gate=ReviewGate(repo, tests, reviewer, target=args.target, scratch=scratch,
                        focused_context=args.review_context == "focused",
                        probe_cmd=DEFAULT_PROBE_CMD if args.review_probes else None),
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
        if r.outcome == "reject" and r.notes:
            print("         " + r.notes.strip().replace("\n", "\n         "))
    print(f"\n--- review notes ---\n{t.review_notes or '(none)'}")
    print(f"\n--- diff ---\n{t.diff or '(none)'}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seam", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--state-dir", type=lambda s: Path(s).expanduser().resolve(),
                        default=Path(".seam").resolve(), help="task store + worktrees")

    pl = sub.add_parser("plan", help="draft a plan for a goal with the LLM planner")
    common(pl)
    pl.add_argument("--repo", required=True, type=lambda s: Path(s).expanduser().resolve())
    pl.add_argument("--goal", required=True, help="the goal, in prose")
    pl.add_argument("--out", required=True, help="where to write the plan JSON")
    pl.add_argument("--target", default="main")
    pl.add_argument("--image", default=DEFAULT_IMAGE)
    pl.add_argument("--model")
    pl.add_argument("--budget-usd", type=float, default=1.0, help="ceiling for planning")
    pl.set_defaults(func=cmd_plan)

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
    r.add_argument("--review-context", choices=["whole", "focused"], default="whole",
                   help="original files for the reviewer: whole, or focused on what the "
                        "diff touches (cheaper; less tested, see doc §8)")
    r.add_argument("--review-probes", action="store_true",
                   help="reviewer also writes adversarial pytest probes; the gate runs "
                        "them in the sandbox and any failure rejects (doc §11)")
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
