# Changelog

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
