# SEAM — Software Engineering Agent Merge

*Many agents, one codebase, one seam.* · Project page: <https://sc0vu.github.io/SEAM/>

SEAM runs several coding agents on one goal in parallel and integrates their work
into one real git repository **without a human in the loop for the normal case**.
A lead splits the goal into typed, file-scoped, dependency-ordered tasks; each worker
implements one task in its own git worktree; an independent reviewer and a serialized
merge engine decide what reaches `main`. Its focus is the part most multi-agent
coders skip: **autonomous integration**.

- No two in-flight tasks touch the same files (the claim enforces it).
- Every change passes an independent review *and* the full test suite on the merged
  tree before it merges.
- A semantic conflict (each branch green alone, broken together) is **escalated to a
  human, never silently merged**.

> **Status: research prototype.** The v1 milestone passes with real agents, and SEAM
> has run on a real codebase (boltons). Every result so far is a handful of runs on
> repos and tasks chosen by the project's author — see [Results](#results) for what
> is and is not shown. Not for unattended use on code you care about yet.

## How it works

```
goal ──▶ LEAD (hand-written plan, or `cli.py plan`: an LLM planner)
           │ typed, file-scoped, dependency-ordered tasks
           ▼
         TASK STORE (sqlite) ── atomic claim: type match, deps merged, scopes disjoint
           │
   ┌───────┴────────┐
 WORKER  …  WORKER     Claude Code headless, one git worktree each, in a Docker sandbox
   │ diff            │
   ▼                 ▼
 REVIEW GATE   deterministic checks (author ≠ reviewer, in scope, task tests green)
   │           then a tool-less LLM reviewer judges diff + original files vs the spec
   ▼
 MERGE ENGINE  ONE task at a time: trial merge → FULL suite on the merged tree
   │             green → main moves (crash-safe) · red or conflict → needs_human
   ▼
 main
```

Each task moves through a state machine (`open → claimed → in_progress → submitted →
in_review → approved → queued_for_merge → merging → merged`, with bounded retries and
`needs_human` as the escalation exit). The full design — states, the claim, the
merge protocol, the invariants — is in **[doc/architecture.md](doc/architecture.md)**;
the project rules are in [CLAUDE.md](CLAUDE.md).

### Safety model

- **Everything agent-written runs in a container**, never on the host: worker runs,
  every test run, the merge-time suite, and the planner. Test runs have no network.
- **Agents never hold your credential.** Workers and the planner run on an internal
  Docker network whose only reachable host is SEAM's credential proxy. They get a
  placeholder token; the proxy swaps in the real one and forwards only
  `POST /v1/messages` (and `count_tokens`) to the Anthropic API. Nothing else is
  reachable, so a prompt-injected agent can neither read the token nor send your code
  anywhere but the model API.
- **Agents never run git.** SEAM stages and commits their edits, with git hooks
  disabled, and a worktree's `.git` is mounted read-only into the container.
- **The reviewer has no tools**: it reads the spec, the diff and the original files
  and returns a verdict; it cannot execute or edit anything.

What it does *not* protect against: a compromised agent can still spend model quota
through the proxy until its run's time and budget caps end it.

## Requirements

- Python ≥ 3.11
- Docker (on macOS, e.g. [Colima](https://github.com/abiosoft/colima)). Colima shares
  only `$HOME` with containers, so keep repos and state dirs under `$HOME`.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) on the host (the reviewer
  uses your normal `claude` login).
- One credential for the agents, as an environment variable — **exactly one** of:
  - `CLAUDE_CODE_OAUTH_TOKEN` (a Claude Pro/Max subscription; create it with
    `claude setup-token`), or
  - `ANTHROPIC_API_KEY`.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
colima start                              # or start Docker another way
docker build -t seam-sandbox sandbox/     # python + pytest + Claude Code

# set the credential without it landing on screen or in shell history
read -s "CLAUDE_CODE_OAUTH_TOKEN?token: " && export CLAUDE_CODE_OAUTH_TOKEN
```

Never commit the credential; `.env` and `*.local.*` are gitignored.

## Quick start: the two v1 cases

[examples/greeter](examples/greeter/README.md) is a four-file repo with two plans:
case 1 (two disjoint tasks → both merge, zero human touch) and case 2 (a deliberately
faulty plan whose tasks conflict semantically → one merges, the other is escalated).

```sh
examples/greeter/make_repo.sh ~/seam-demo/case1
.venv/bin/python cli.py run --repo ~/seam-demo/case1 --plan examples/greeter/case1.json \
    --state-dir ~/seam-demo/case1-state --budget-usd 3 --workers 2
.venv/bin/python cli.py status --state-dir ~/seam-demo/case1-state
```

SEAM moves the target branch itself, so run it on a **dedicated clone** whose target
branch is not checked out (`make_repo.sh` leaves `HEAD` detached).

## Commands

| command | what it does |
|---|---|
| `cli.py plan --repo R --goal "…" --out plan.json` | the LLM planner drafts a plan from a goal in prose (read-only, in the sandbox) and writes it for you to review or edit |
| `cli.py run --repo R --plan plan.json` | load the plan and run the team until nothing more can progress |
| `cli.py run --repo R` | resume the tasks already in the state dir |
| `cli.py add-goal --plan plan.json` | load a plan without running it |
| `cli.py status` | task states, the merge slot and queue, escalations, cost and tokens |
| `cli.py inspect --task N` | one task: branch, diff, every run with its cost, rejection notes |

All take `--state-dir` (default `./.seam`). Useful `run` options:

| option | default | meaning |
|---|---|---|
| `--target` | `main` | the branch SEAM merges into |
| `--workers` | 2 | workers per task type |
| `--budget-usd` | 5 | ceiling for the whole run; no new agent run starts once it is spent |
| `--run-budget-usd` | 1 | ceiling per worker run |
| `--suite-cmd` | `python -m pytest -q` | the FULL suite run on every merged tree |
| `--model`, `--reviewer-model` | Claude Code's default | models for workers / reviewer |
| `--review-context` | `whole` | `focused` sends large files as an outline plus the code the diff touches (cheaper, less tested) |
| `--review-probes` | off | the reviewer also writes adversarial pytest probes; any failure rejects (see Results) |

A plan is JSON: a `goal` and `tasks`, each with `key`, `type`, `description`,
`spec_ref` (the contract it implements), `file_scope` (globs, including its tests),
`blocked_by` (task keys that must merge first) and `test_cmd`. See
[examples/boltons/plan2.json](examples/boltons/plan2.json).

**Cost.** On a subscription, the `$` figures are Claude Code's reported
API-equivalent cost and count against your plan's usage limits; with an API key they
are billed. Measured so far: greeter case 1 ≈ $0.05, boltons goals $0.67–$1.35
(see Results). `run` prints cost and tokens by role; `inspect` per run.

**Commits.** Everything SEAM commits uses one identity (`SEAM merge engine`); which
worker wrote what is in the trailers: `git log --grep 'SEAM-Worker: backend-2'`.

## Results

All on 2026-10-30, Claude Code workers, Opus 5.5 reviewer; full detail and caveats in
[doc/architecture.md §8, §11, §12](doc/architecture.md).

| run | outcome |
|---|---|
| greeter case 1 (1 and 2 workers) | both tasks merged, zero touch; two workers ran in parallel |
| greeter case 2 | semantic conflict caught by the merge-time suite and escalated; `main` never broke |
| boltons-bench-1: 8 independent features | 8/8 merged, 0 escalated; `master` 807 tests green; $0.67 |
| boltons-bench-2: 5 tasks with dependencies and a shared file | 5/5 merged in the right order; the reviewer rejected 2 real bugs, both fixed on retry; $1.35 |
| LLM planner on the bench-2 goal | 2-task plan with the right dependency; found the coupling on its own; $0.07 |

What these do **not** show:

- **An escalation rate on real code.** No real-code task has escalated yet. The plans
  were written by the same AI that built SEAM, and careful hand plans rarely contain
  the mistakes that cause escalations. The LLM planner exists to measure this
  honestly; its first plan has not been run yet.
- **A reliable reviewer.** Re-reviews showed a single LLM review let a significant,
  test-passing bug through about half the time. Adversarial probe tests caught 4/4
  buggy versions but also falsely rejected 3/4 correct ones (their assertions encoded
  pre-existing quirks of the original code), so probes stay opt-in.
- **Anything statistical.** Each result is 1–3 runs.

## Layout

```
coordinator/   the lead: plans → tasks, scope rulings, budget; Planner interface
planners/      planner runtime adapter (Claude Code)
engine/        task state machine, git ops, the serialized merge engine, orchestrator
workers/       Worker interface + Claude Code adapter
reviewer/      the review gate + Claude Code reviewer adapter
sandbox/       Docker sandbox, credential proxy, sandbox image
store/         sqlite task store: atomic claim, single merge slot, run accounting
examples/      greeter (v1 cases), boltons (real-repo benchmarks)
doc/           architecture.md — the design and every measurement
cli.py         plan | run | add-goal | status | inspect
```

## Development

```sh
.venv/bin/pytest && .venv/bin/ruff check .
```

The engine, coordinator and state machine import no model runtime, so they are
unit-tested without model calls; the Docker tests run when a daemon is available.
Rules for changes (tests and docs in the same change, keep `doc/architecture.md` in
sync, one logical change per commit) are in [CLAUDE.md](CLAUDE.md).

## License

SEAM is licensed under the **GNU Affero General Public License v3.0 only**
(`AGPL-3.0-only`); see [LICENSE](LICENSE). If you distribute SEAM or a modified
version, or let others use a modified version over a network, you must make its
source available under the same license.
