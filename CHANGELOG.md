# Changelog

## 0.2.2 — 2026-11-13

- **Empty repositories.** `seam plan` plans a from-scratch goal against an empty
  snapshot instead of failing at `rev-parse main^{commit}`. `seam run` refuses up
  front with the one command that fixes it (`git commit --allow-empty -m "initial
  commit" && git checkout --detach`), instead of clone-and-detach advice that cannot
  work on a branch with no commits. Empty trees export cleanly, and a pytest suite
  that collects no tests passes the starting check (only; merges stay strict).
- **Unchecked claims.** `seam plan` lists each task whose contract states current
  behavior ("existing", "unchanged", ...) without a probe to check it, as UNCHECKED,
  and records them in the plan (`unprobed_claims`).
- **Test commands vs the full suite.** `seam plan --suite-cmd` tells the planner what
  the merge suite runs and warns for a task with no `test_cmd`, or a pytest `test_cmd`
  missing the suite's doctest flags (it could pass review, then fail at merge);
  `seam run --plan` repeats the warning.

## 0.2.1 — 2026-11-12

Fixes for running more than one state dir on the same repository.

- **Task branches no longer collide.** They were always `seam/task-<id>`, and ids
  restart at 1 in every state dir, so a second goal in a fresh `--state-dir` (or a
  run on another `--target`) on the same repo failed with `git worktree add`, leaving
  the task stuck. Branches are now `seam/<namespace>/task-<id>`, with a random
  namespace kept per state dir. Merged branches are deleted; escalated ones are kept.
- **Tasks stuck in `claimed` are started again.** A task left `claimed` by a run that
  stopped early (a crash, or the collision above) was never picked up and held its
  files. The next `run` now starts it, reusing a surviving worktree.

Upgrading: existing state dirs gain a namespace on first use; tasks already in
progress keep their branches.

## 0.2.0 — 2026-11-12

Guards against the failures found by replaying 7 merged boltons fixes, goals and
judging tests written outside the project (`examples/boltons-replay/`). Every
measured result is still a handful of runs; see `doc/architecture.md` §12.

- **Existing-test guard.** The review gate rejects, before tests run or a model is
  paid, a change that deletes a pre-existing test, drops one of its original
  statements, or silences it (skip/xfail markers, an early return). A plan allows a
  deliberate behavior change per task with `may_change_tests`. In the replay a wrong
  contract plus a rewritten test had merged an API break with the suite green.
- **Claim probes.** `seam plan` asks the planner to back each statement about the
  code's current behavior with a small pytest probe, runs the probes on the untouched
  code in the sandbox, and reports any that fail, in the plan file (`claim_check`)
  and on the terminal, for the human to read before `run`.
- **Proposals.** A worker that finds a bug the task did not ask for describes it in
  `.seam-proposals` instead of fixing it out of scope; the reviewer judges the
  deferral, and `status` / `inspect` list the proposals. They never become tasks on
  their own: the lead stays the only task producer.
- **Benchmark.** `examples/boltons-replay/`: goals from contributor PRs, SEAM starting
  from the fix's parent, the maintainers' own tests as a hidden check. Rerun with the
  guards: 12 of 13 tasks merged, 0 regressions, 27/27 hidden target tests.

Upgrading: state dirs from 0.1.0 are migrated in place on first use, and 0.1.0 plan
files still load (the new fields are optional).

## 0.1.0 — 2026-11-12

First release on PyPI, as `seam-merge` (the `seam` name there is an unrelated SDK).
**Alpha, a research prototype**: every measured result is a handful of runs on repos
and tasks chosen by the author (see the README's Results); not for unattended use on
code you care about.

- **Engine.** Task state machine; sqlite store with an atomic, file-scope-disjoint
  claim and a single global merge slot; crash-safe serialized merge engine that
  escalates semantic conflicts (each branch green, the merged tree red) to a human.
- **Two gates before merge.** An independent reviewer runs the task's tests on its
  branch; the merge engine runs the full suite on the merged tree. Optional
  adversarial probe tests (`--review-probes`).
- **Lead.** Hand-written plans, or an LLM planner (`seam plan`) whose drafts pass
  the same validation. `seam amend` changes a goal's direction.
- **Human rulings.** `seam resolve | rework | rereview | cancel` for escalated tasks;
  `seam-mcp` exposes the CLI as MCP tools over stdio.
- **Safety.** Workers and every test run execute in Docker; agents never hold the
  real credential (a credential proxy does, with a token cap). `run` refuses a red
  target before any spend.
- **Commits.** Authored by the operator, committed by SEAM, models credited with
  `Co-Authored-By`.
- **Install.** `pipx install seam-merge`, then `seam image build` builds the sandbox
  image locally (Claude Code 2.1.312 pinned). macOS and Linux only.
