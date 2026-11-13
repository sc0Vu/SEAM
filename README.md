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
goal ──▶ LEAD (hand-written plan, or `seam plan`: an LLM planner)
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
merge protocol, the invariants — is in **[doc/architecture.md](https://github.com/sc0Vu/SEAM/blob/main/doc/architecture.md)**;
the project rules are in [CLAUDE.md](https://github.com/sc0Vu/SEAM/blob/main/CLAUDE.md).

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

- **The proxy caps tokens** (`--token-cap`): it meters the usage every model response
  reports and refuses generation once the run's cap is spent, a limit the agents
  cannot lift.

What it does *not* protect against: a compromised agent can still spend model quota
through the proxy up to that cap.

## Requirements

- macOS or Linux (not Windows), Python ≥ 3.11
- Docker (on macOS, e.g. [Colima](https://github.com/abiosoft/colima)). Colima shares
  only `$HOME` with containers, so keep repos and state dirs under `$HOME`.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) on the host (the reviewer
  uses your normal `claude` login).
- One credential for the agents, as an environment variable — **exactly one** of:
  - `CLAUDE_CODE_OAUTH_TOKEN` (a Claude Pro/Max subscription; create it with
    `claude setup-token`), or
  - `ANTHROPIC_API_KEY`.

## Install

```sh
pipx install seam-merge                   # or: uv tool install seam-merge
colima start                              # or start Docker another way
seam image build                          # the sandbox image: python + pytest + Claude Code

# set the credential without it landing on screen or in shell history
read -s "CLAUDE_CODE_OAUTH_TOKEN?token: " && export CLAUDE_CODE_OAUTH_TOKEN
```

Never commit the credential; `.env` and `*.local.*` are gitignored.

The package is `seam-merge` on PyPI (`seam` is an unrelated project); it installs the
`seam` and `seam-mcp` commands and has no Python dependencies.

## Quick start: the two v1 cases

The examples live in this repository, so clone it first
(`git clone https://github.com/sc0Vu/SEAM && cd SEAM`).
[examples/greeter](https://github.com/sc0Vu/SEAM/blob/main/examples/greeter/README.md) is a four-file repo with two plans:
case 1 (two disjoint tasks → both merge, zero human touch) and case 2 (a deliberately
faulty plan whose tasks conflict semantically → one merges, the other is escalated).

```sh
examples/greeter/make_repo.sh ~/seam-demo/case1
seam run --repo ~/seam-demo/case1 --plan examples/greeter/case1.json \
    --state-dir ~/seam-demo/case1-state --budget-usd 3 --workers 2
seam status --state-dir ~/seam-demo/case1-state
```

SEAM moves the target branch itself, so run it on a **dedicated clone** whose target
branch is not checked out (`make_repo.sh` leaves `HEAD` detached). Starting from an
empty repository? `seam plan` works as is; before `seam run`, give it a first commit
and free the branch: `git commit --allow-empty -m "initial commit" && git checkout --detach`.

## Commands

| command | what it does |
|---|---|
| `seam plan --repo R --goal "…" --out plan.json` | the LLM planner drafts a plan from a goal in prose (read-only, in the sandbox) and writes it for you to review or edit |
| `seam plan --repo R --base plan.json --goal "…" --out plan2.json` | revise an existing plan (e.g. one you edited) instead of starting over: keeps its tasks unless the goal needs them changed, adds what it asks for, and lists every task removed, changed or added |
| `seam run --repo R --plan plan.json` | load the plan and run the team until nothing more can progress |
| `seam run --repo R` | resume the tasks already in the state dir |
| `seam add-goal --plan plan.json` | load a plan without running it |
| `seam status` | task states, the merge slot and queue, escalations, cost and tokens |
| `seam proposals [--out goal.txt]` | draft a goal from the bugs workers set aside instead of fixing out of scope; agent-written, so read it before `seam plan` |
| `seam inspect --task N` | one task: branch, diff, every run with its cost, rejection notes |
| `seam resolve --task N` | requeue an escalated task whose branch a review approved (it escalated at merge); the full suite still runs |
| `seam rework --task N --notes "…"` | send an escalated task back to a worker with your guidance |
| `seam rereview --task N` | after fixing the sandbox image, review a task whose tests could not run in it |
| `seam cancel --task N` | cancel a task and everything that depends on it |
| `seam amend --from-goal A --plan new.json` | change direction: cancel goal A's unstarted tasks, keep merged and in-flight work, load the new plan; A's spend counts toward the new goal's budget |

Commands that change task state refuse to run while another one (usually a `run`)
holds the state dir, so a human ruling never races a running team.

Before any agent starts, `run` runs `--suite-cmd` on the target's committed tree in the
sandbox and refuses if it fails, at no model cost. Nothing can merge into a red target.
Typical causes: the image lacks the project's toolchain (build one with it and pass
`--image`), or files the build needs are untracked or gitignored (SEAM tests commits).

All take `--state-dir` (default `./.seam`). Useful `run` options:

| option | default | meaning |
|---|---|---|
| `--target` | `main` | the branch SEAM merges into |
| `--workers` | 2 | workers per task type |
| `--budget-usd` | 5 | ceiling for the goal, including earlier runs of it; no new agent run starts once it is spent |
| `--run-budget-usd` | 1 | ceiling per worker run |
| `--token-cap` | 2,000,000 | metered tokens the credential proxy forwards for all workers of the run (cache reads count a tenth) |
| `--suite-cmd` | `python -m pytest -q` | the FULL suite run on every merged tree |
| `--model`, `--reviewer-model` | Claude Code's default | models for workers / reviewer |
| `--review-context` | `whole` | `focused` sends large files as an outline plus the code the diff touches (cheaper, less tested) |
| `--author` | the repo's git `user.name <user.email>` | who the run's commits are authored by |
| `--review-probes` | off | the reviewer also writes adversarial pytest probes; any failure rejects (see Results) |

A plan is JSON: a `goal` and `tasks`, each with `key`, `type`, `description`,
`spec_ref` (the contract it implements), `file_scope` (globs, including its tests),
`blocked_by` (task keys that must merge first), `test_cmd`, and optionally
`may_change_tests` (existing tests, `path::name`, the task may rewrite because it
deliberately changes their behavior; the review gate rejects any other rewrite). See
[examples/boltons/plan2.json](https://github.com/sc0Vu/SEAM/blob/main/examples/boltons/plan2.json).

**Cost.** On a subscription, the `$` figures are Claude Code's reported
API-equivalent cost and count against your plan's usage limits; with an API key they
are billed. Measured so far: greeter case 1 ≈ $0.05, boltons goals $0.67–$1.35
(see Results). `run` prints cost and tokens by role; `inspect` per run.

**Commits.** You are the author of what your run merges (`--author`, else your git
`user.name`/`user.email`; `SEAM merge engine` if neither is set), and SEAM is the
committer. Each task commit credits the models that wrote it with `Co-Authored-By`,
and the trailers say which worker ran them: `git log --grep 'SEAM-Worker: backend-2'`.

## Use it from Claude Code or Claude Desktop (MCP)

`seam-mcp` exposes SEAM as MCP tools: `status`, `inspect`, the rulings
(`resolve`, `rework`, `rereview`, `cancel`), plans (`draft_plan`, `save_plan`, `show_plan`),
`amend`, `run`, and `job` (a background job's log). Register it with Claude Code:

```sh
claude mcp add seam -- seam-mcp \
    --state-dir ~/seam-demo/x-state --repo ~/seam-demo/x --max-budget-usd 3
```

Limits are set here, at launch, not by the client: one state dir, the repos a client
may plan or run on (`--repo`, repeatable), and the most a single run or plan may spend.
Every spending call must also give its own `budget_usd`. `run` and `draft_plan` start
`seam` in the background, and `run` holds the state dir's writer lock as usual. The
server inherits its environment from the client that starts it, so export the
credential in the shell you start the client from; don't put it in the MCP config.
SEAM opens no network port. To work remotely, use your client's own remote access
(e.g. Claude Code's Remote Control).

## Results

2026-10-30 to 2026-11-12, Claude Code workers, Opus 5.5 reviewer; full detail and caveats in
[doc/architecture.md §8, §11, §12](https://github.com/sc0Vu/SEAM/blob/main/doc/architecture.md).

| run | outcome |
|---|---|
| greeter case 1 (1 and 2 workers) | both tasks merged, zero touch; two workers ran in parallel |
| greeter case 2 | semantic conflict caught by the merge-time suite and escalated; `main` never broke |
| boltons-bench-1: 8 independent features | 8/8 merged, 0 escalated; `master` 807 tests green; $0.67 |
| boltons-bench-2: 5 tasks with dependencies and a shared file | 5/5 merged in the right order; the reviewer rejected 2 real bugs, both fixed on retry; $1.35 |
| LLM planner on the bench-2 goal | 2-task plan with the right dependency; found the coupling on its own; $0.07 |
| that plan, run unedited | 2/2 merged, 0 escalated — but 2 test-passing defects shipped (one inherited from a boltons bug); $0.47 |
| LLM planner on the bench-1 goal, run unedited | same 8-task plan as the hand-written one; 8/8 merged, 0 escalated; probes found 1 defect, inherited from a boltons bug that bench-1's hand-planned run also hit; $1.44 |
| replay rerun with the existing-test guard and claim probes | 13 tasks, 12 merged, 1 escalated (r1 again, again a correct branch); 27/27 hidden target tests where work merged; 0 regressions; $2.73 |
| replay: 7 merged boltons fixes, goals from contributor PRs, judged by the maintainers' hidden tests | 15 tasks, 14 merged, 1 escalated (a false escalation: its branch was correct); 27/27 hidden target tests pass where work merged; 1 silent API break, caused by a wrong planner contract that the reviewer enforced; $2.91 |

What these do **not** show:

- **An escalation rate on real code.** One real-code task has escalated (1 of 38), and
  it was a false escalation. The replay goals come from outside the project, but the
  sample is 7 goals; too few to call a rate.
- **That merged means correct.** Behavioral probes, run after the fact, found
  test-passing defects on master after both LLM-planned runs.
- **A reliable reviewer.** Re-reviews showed a single LLM review let a significant,
  test-passing bug through about half the time. Adversarial probe tests caught 4/4
  buggy versions but also falsely rejected 3/4 correct ones (their assertions encoded
  pre-existing quirks of the original code), so probes stay opt-in.
- **Anything statistical.** Each result is 1–3 runs.

## Layout

```
seam_merge/       the import package (PyPI: seam-merge)
  coordinator/   the lead: plans → tasks, scope rulings, budget; Planner interface
  planners/      planner runtime adapter (Claude Code)
  engine/        task state machine, git ops, the serialized merge engine, orchestrator
  workers/       Worker interface + Claude Code adapter
  reviewer/      the review gate + Claude Code reviewer adapter
  sandbox/       Docker sandbox, credential proxy, sandbox image
  store/         sqlite task store: atomic claim, single merge slot, run accounting
  cli.py         `seam`: plan | run | add-goal | amend | status | inspect | resolve | rework | cancel
  mcp_server.py  `seam-mcp`: the same, as MCP tools over stdio
examples/        greeter (v1 cases), boltons (real-repo benchmarks)
doc/             architecture.md — the design and every measurement
```

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest && .venv/bin/ruff check .
```

Releases: bump `version` in `pyproject.toml`, add a `CHANGELOG.md` entry, then push a
`v<version>` tag; `.github/workflows/release.yml` publishes to PyPI.

The engine, coordinator and state machine import no model runtime, so they are
unit-tested without model calls; the Docker tests run when a daemon is available.
Rules for changes (tests and docs in the same change, keep `doc/architecture.md` in
sync, one logical change per commit) are in [CLAUDE.md](https://github.com/sc0Vu/SEAM/blob/main/CLAUDE.md).

## License

SEAM is licensed under the **GNU Affero General Public License v3.0 only**
(`AGPL-3.0-only`); see [LICENSE](https://github.com/sc0Vu/SEAM/blob/main/LICENSE). If you distribute SEAM or a modified
version, or let others use a modified version over a network, you must make its
source available under the same license.
