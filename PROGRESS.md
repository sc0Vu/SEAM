# SEAM progress log

Last updated: 2026-11-13. State: released **seam-merge 0.2.2** on PyPI (tag v0.2.2);
396 tests passing, ruff clean. Design and every measurement: `doc/architecture.md` (§8, §10–§12).

## Done

### v1 milestone (CLAUDE.md) — met
- Engine: task state machine, sqlite store with atomic disjoint claim and single
  merge slot, glob-overlap scopes, crash-safe merge engine, orchestrator.
- Lead (hand-written plans), Claude Code worker and reviewer adapters, Docker sandbox,
  CLI (`plan`, `add-goal`, `run`, `status`, `inspect`).
- Greeter case 1 (happy path) and case 2 (escalation) pass with real agents; case 1
  also with 2 parallel workers.

### Safety
- Credential proxy: workers and the planner run on an internal Docker network and get
  a placeholder token; only the proxy holds the real one and forwards only
  `POST /v1/messages` and `count_tokens`. Isolation tested with real containers.
- `--pull never` on every container (no implicit image pulls).
- Interpreter byproducts (`__pycache__`, `*.pyc`, `.pytest_cache`) never staged.
- Proxy token cap (`--token-cap`, run 2M / plan 500k): meters response usage (cache
  reads at 1/10), refuses generation with 429 once spent.
- Linux: containers run as the host uid:gid with HOME=/tmp.

### Human in the loop
- `cli.py resolve | rework | cancel` for escalated tasks; resolve refused unless a
  review approved the branch as-is (invariant 5). Rulings recorded as `human` runs.
- Writer lock (`flock`) on the state dir: every state-writing command; `status` shows
  the holder.
- `mcp_server.py`: the CLI as MCP tools over stdio; state dir, repos and per-call
  spend cap fixed at launch. Checked against the official mcp 2.3.0 client.
  A job's exit is logged by the job's own process, so `job` reports it even after
  the server exits or restarts (lost before: found in the case 2 run below).
- Owner decision 2026-11-11: no AI resolver for escalations; keep the current design.

### Robustness
- Denied scope requests stored with the task (`pending_scope`), retried after restart.
- A goal's budget resumes from the `runs` table, so restarting `run` never resets it.
- A task whose tests cannot run in the sandbox (exit 125-127: image lacks a tool)
  escalates at once without spending an attempt; `rereview` re-sends the unchanged
  branch to review, allowed only after that environment failure.
- `run` checks the full suite on the target before loading or spending, and refuses a
  red target. When a merged tree fails, the engine also tests the target alone, so a
  broken target is not reported as a semantic conflict.

### Accounting and cost
- `runs` table: cost, tokens (input / cache read / cache write / output), review
  notes and probes per agent run; shown by `run`, `status`, `inspect`.
- Worker tools limited to Bash/Edit/Glob/Grep/Read/Write (−45% input tokens).
- Reviewer: rules as system prompt, host settings/plugins not loaded, prompt caching
  off, one-sentence approvals.

### Review gate
- Reviewer gets the changed files' original text (whole by default;
  `--review-context focused` opt-in).
- Adversarial probe tests (`--review-probes`, opt-in): reviewer writes pytest probes,
  the gate runs them in the sandbox, any failure rejects.

### AI lead
- `Planner` interface (`coordinator/planner.py`), sandboxed Claude Code planner
  (`planners/claude_code.py`, read-only tools, structured output), `cli.py plan`.
- Plans validated by `plan_from_dict`, same rules as hand-written plans.

### Project
- README, AGPL-3.0-only license, GitHub Pages site (`site/`, deploys on push),
  CI (ruff + pytest on 3.11–3.13, plus a sandbox-image job).
- Commit trailers `SEAM-Task` / `SEAM-Worker` / `SEAM-Attempt`.
- Commit identity (2026-11-12): author = the operator (`run --author`, else git
  user, else SEAM), committer = SEAM; task commits add `Co-Authored-By` per model
  the worker used. No GitHub account for SEAM (a GitHub App once it pushes).
  Seen on real agent output in greeter case 2 (2026-11-12).
- Lead refuses to load the same goal twice; scope requests count toward the retry
  bound.

### Release 0.2.2 (2026-11-13)
- Empty repositories: `plan` plans from an empty snapshot; `run` refuses with the one
  command that fixes it; empty trees export; "no tests collected" passes the baseline
  only. Reported by the owner (`rev-parse main^{commit}` on an empty repo).
- `seam plan` flags statements about current behavior with no probe (UNCHECKED), and
  test_cmds missing the full suite's doctest flags (`--suite-cmd`, told to the planner).

### Release 0.2.1 (2026-11-12)
- Task branches namespaced per state dir (`seam/<ns>/task-<id>`); merged ones deleted;
  tasks stuck in `claimed` are started again. Found by asking whether `--target`
  changes branch names: two state dirs on one repo collided on `seam/task-1`.

### Release 0.2.0 (2026-11-12)
- Existing-test guard, claim probes in `seam plan`, worker proposals; see CHANGELOG.

### Release 0.1.0 (2026-11-12)
- `pipx install seam-merge`; commands `seam`, `seam-mcp`; import package `seam_merge`
  (`seam` on PyPI is an unrelated SDK). `seam image build` builds the sandbox image
  from the packaged Dockerfile (Claude Code 2.1.312 pinned).
- CI tests the installed wheel with the source tree moved away. That test found the
  credential proxy failed for installs outside `$HOME` under colima (fixed: source
  passed inline instead of bind-mounted).
- Releases: bump version + CHANGELOG, push `v<version>`; `release.yml` publishes via
  PyPI trusted publishing (no stored token) and creates the GitHub release.

## Results (single runs; author-chosen repos and tasks)

| run | outcome | cost |
|---|---|---|
| greeter case 1 | 2/2 merged, zero touch | ≈ $0.05 |
| greeter case 2 | semantic conflict escalated, main never broke | $0.21 |
| boltons-bench-1: 8 independent features | 8/8 merged, 807 tests green | $0.67 |
| boltons-bench-2: 5 dependent tasks | 5/5 merged in order; 2 real bugs rejected and fixed | $1.35 |
| LLM planner on the bench-2 goal | sound 2-task plan, found the coupling unaided | $0.07 |
| that plan, run unedited | 2/2 merged, 0 escalated, 782 tests green — **but 2 real bugs shipped** | $0.47 |
| LLM planner on the bench-1 goal, run unedited (bench4) | same 8-task plan; 8/8 merged, 806 green; probes: 1 inherited defect | $1.44 |
| replay: 7 merged boltons fixes (goals from contributor PRs, maintainers' hidden tests) | 15 tasks: 14 merged, 1 false escalation; 27/27 hidden target tests where merged; 1 silent API break from a wrong planner contract | $2.91 |
| replay rerun with the existing-test guard + claim probes | 13 tasks: 12 merged, 1 false escalation (r1 again); 27/27 hidden where merged; 0 regressions | $2.73 |
| greeter case 2, driven entirely over MCP (2026-11-12) | farewell merged; rename escalated as a semantic conflict, main never broke; cancelled over MCP | $0.12 |

## Key findings
- **The review gate is the weak point.** A single LLM review let a significant,
  test-passing bug through about half the time; the LLM-planned run shipped two
  such bugs (stale `qp`/`query_params` on URLs returned by the new query methods;
  `setlist(nan, …)` silently dropping values) past tests, review and the merge suite.
- **Probe tests catch those bugs** (4/4 buggy versions) **but falsely reject 3/4
  correct ones**: the probes asserted pre-existing quirks of the original code.
- **Escalation rate alone is misleading**: a run can merge everything and still ship
  defects. A silent-defect check is needed alongside merged / needs_human.
- First real-code escalation (replay r1), and it was false: the branch was correct.
- Replay (independent goals): the planner decomposes well; its *contract* is the weak
  point. r3's planner invented "missing source raises", the reviewer enforced it, the
  worker rewrote an existing test to match, and an API break merged. r1's planner
  added an unrequested test that led to a 3-rejection loop; the worker had no way to
  propose the side bug as a separate task.
- boltons' `qp` alias discards in-place `query_params` edits when read; all three URL
  features (bench-1, 3, 4) fell into it, and no review caught it. It accounts for one
  of bench-3's two "real bugs" too (root cause predates SEAM).

## Open issues
- Reviewer reliability (above).
- A compromised agent can still spend quota through the proxy up to `--token-cap`.
- Planner gap seen once: task `test_cmd` omitted doctests the merge suite runs.

## Pending decisions (owner)
- Probe adjudication: on a probe failure, one extra reviewer call with the original
  code decides whether the change caused it. Fixes false rejections but ends
  "a failing probe always rejects".
- GitHub Pages: Settings → Pages → Source: "GitHub Actions" (needed for the site).
- `gh` CLI login has expired (`gh auth login`), so CI logs can't be read from here.

## Next
0. From the replay: (a) DONE 2026-11-12: existing-test guard + `may_change_tests`
   (r3 rerun merged clean, no regression); (b) DONE 2026-11-12: claim probes, run by
   `seam plan` on the untouched code (17 probes on 7 replay goals: 1 true catch,
   r1's trap; 2 bad probes; 6 of 13 tasks unprobed); (c) DONE 2026-11-12:
   proposals (`.seam-proposals`): the exact plan that escalated r1 now merges first
   try with the side bug recorded as a proposal (1 controlled trial).
1. Probe adjudication — deferred by owner (2026-11-11).
2. A second-codebase replay benchmark (goals the planner has not seen) — skipped by
   owner 2026-11-12; plan-and-run in one step stays off until the lead is measured.
3. Done 2026-11-12: greeter case 2 over MCP (save_plan, run, status, inspect,
   cancel); commit identity checked on real output; no credential found in process
   args, state dir, repo files or git objects.

## Spend
2026-11-12: $0.12 (greeter case 2 over MCP) + $2.91 (replay benchmark, 7 goals) + $0.21 (r3 rerun) + $0.72 (two claim-probe re-plans) + $2.73 (full replay rerun) + $0.37 (r1 proposal trials).
2026-11-11: $1.44 (bench4 planning $0.08 + run $1.36).
Before: ≈ $6 API-equivalent so far (subscription usage): $2.72 in run records (bench1 $0.67,
bench2 $1.35, bench3 $0.47, token-measurement runs $0.24), plus ≈ $0.80 of greeter runs
from before run recording, $0.07 planning, and ≈ $2.40 of reviewer measurements made
outside runs (probes $1.48, context re-reviews ≈ $0.90). The last two are estimates.
