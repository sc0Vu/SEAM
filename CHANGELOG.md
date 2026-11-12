# Changelog

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
