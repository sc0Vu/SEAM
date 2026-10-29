# CLAUDE.md — SEAM

**SEAM** (Software Engineering Agent Merge): a multi-agent system where one
coordinator decomposes a goal into tasks and many specialized agents implement them
in parallel — each in its own git worktree — while an independent reviewer and a
serialized merge engine integrate their work into one real repository. SEAM's focus
is the part most multi-agent coders skip: **autonomous integration**. No two agents
touch the same files, every change passes an independent review and a full test
suite before it merges, and true semantic conflicts are escalated to a human rather
than silently merged. *Many agents, one codebase, one seam.*

Full design: **doc/architecture.md — read it before any work on the engine, the
task state machine, the merge/review protocol, or the task schema.** This file is
the always-loaded constitution; the architecture doc is the detail.

The owner reasons from first principles: before a change, name the constraint it
serves (conflict prevention, serialization, independence of review, safety) rather
than copying another agent framework.

## Current milestone (work on THIS)
**v1 walking skeleton: 1 lead + 2 workers + 1 reviewer + the merge engine**, proven
on two cases on a real small repo:
1. **Happy path** — two tasks with disjoint file_scope merge green, zero human touch.
2. **Escalation path** — two tasks touching a shared interface must ESCALATE
   (merge-time full suite catches the semantic conflict) — never silently merge a break.
Success = case 1 fully autonomous, case 2 correctly escalated (caught, not merged
broken). Do not add worker types, Postgres, a UI, or the architect role until both pass.

## Non-negotiable invariants (stop and flag if a task would violate one)
1. **Worktree isolation.** Every in-flight task runs in its own git worktree +
   branch. Workers never edit the main working tree directly.
2. **File-scope disjointness.** No two in-flight tasks share a file_scope. The claim
   enforces it. This is the conflict-*prevention* core — most merges stay trivial.
3. **No silent scope escape.** A worker that must touch files outside its declared
   file_scope STOPS and requests expansion from the lead, which re-checks
   disjointness before granting. It never edits outside its scope silently.
4. **`merging` is a single global slot.** At most one task merges at a time; each
   merge changes main, so the next rebases onto the new main and re-runs tests.
5. **Two gates before merged.** A task reaches `merged` only after (a) an independent
   review AND (b) a merge-time FULL test-suite run on the merged tree. These are two
   distinct runs: the reviewer runs the task's own tests on the branch; the merge
   engine runs the whole suite on the merged result.
6. **Reviewer independence.** The reviewer is never the authoring worker and reviews
   the diff against the spec/contract, not the worker's own claims.
7. **One lead = sole task producer**, and sole owner of the interface contract.
   Workers never change a shared interface unilaterally — that goes through the lead.
8. **Worker runtime is pluggable.** A worker is `run(task, worktree) -> diff` behind
   one interface. No runtime-specific code (Claude Code / AgentScope specifics) leaks
   into the engine, coordinator, or state machine.
9. **Semantic conflicts escalate, never auto-resolve.** If the merged tree fails the
   full suite with no textual conflict, it goes to `needs_human`. Judgment lives there.
10. **Sandboxed execution.** Agent-WRITTEN code, build steps, and all test runs
    execute in a sandbox (container), never on the host. A worker's diff is data
    until it has passed review and merge; its tests are untrusted code.
11. **Bounded retries.** A task that fails review a fixed number of times (default 3)
    goes to `needs_human` — it never loops `changes_requested` forever. A merge
    failure (textual or semantic) escalates on the FIRST failure; there is no merge
    retry loop to bound.
12. **Crash-safe merge.** Main moves only after both gates pass, and the merge commit
    is recorded before it does. On restart, a task stuck in `merging` whose merge
    commit is on main is finished as `merged`; any other is requeued. A partial merge
    never reaches main.
13. **Secrets via env only.** LLM/agent API keys from env vars; never hard-coded or
    committed. `.env` and `*.local.*` gitignored.

## Tech (v1 choices — revisable, flagged as such)
- **Python** orchestrator + engine (matches worker runtimes; fastest to skeleton).
- **SQLite** task store in v1 — valid ONLY while a single orchestrator process is the
  sole writer of task state. Switch to **Postgres** the moment worker processes write
  task state directly (concurrent writers → `FOR UPDATE SKIP LOCKED`). The deciding
  variable is concurrent writers, not scale.
- **git worktrees** for per-task isolation; **a container sandbox** (Docker) for
  running worker code and tests.
- **Worker runtime v1 = Claude Code headless** (already does worktree-style
  isolation), behind the pluggable `Worker` interface. AgentScope is the alternate.

## Layout
```
coordinator/     the lead: goal → typed, file-scoped, dependency-ordered tasks; contract owner
engine/          the task state machine + the SERIALIZED merge+review engine (the novel core)
workers/         the pluggable Worker interface + runtime adapters (claude_code, agentscope)
reviewer/        the independent review gate
sandbox/         container execution for worker code + test runs
store/           task store (sqlite v1 → postgres); schema + atomic claim
contract/        the frozen interface contract the lead owns (workers build against it)
doc/
  architecture.md   FULL design — source of truth for the architecture
cli.py           entrypoint: run | status | add-goal | inspect
```

## Commands
```
python cli.py run --goal "<goal>" --repo <path>     # launch a team on a repo
python cli.py status                                 # task states, merge queue, escalations
python cli.py inspect --task <id>                    # one task's branch, diff, review notes
pytest && ruff check .                               # test + lint gate
```

## Definition of done (every change)
- Imports/builds; `pytest` and `ruff check .` pass.
- **Tests ship in the same change.** Unit tests for engine logic (state transitions,
  the claim, the merge gate, retry bound, crash-recovery). The two v1 cases are the
  engine's integration regression tests. Assert against expected states/outcomes
  derived independently — never a test that just mirrors current behavior.
- **Docs in the same change.** Public functions/classes get docstrings stating intent
  and the invariant they uphold.
- **Keep doc/architecture.md in sync.** Any change to the state machine, task schema,
  roles, the claim, or the merge/review protocol updates doc/architecture.md in the
  SAME change. Code and the architecture doc must never disagree.

## Commits
- One commit = one logical change (a transition, the claim, a worker adapter). Feature
  / fix / refactor / rename never bundled.
- Commit as you go; each commit builds and passes tests alone.
- Conventional Commit subject, imperative, ≤72 chars.
- Never commit secrets, `.env`, or `*.local.*`. Never `--force` a shared branch.
- Don't commit unless asked or the task clearly calls for it; propose a split if a
  change spans several logical units.

## Conventions
- Errors: raise typed exceptions in libs; the CLI is the only place that prints/exits.
- The engine and coordinator are plain, testable Python — no LLM/agent-runtime imports
  inside them (those live behind the Worker/Reviewer interfaces), so the state machine
  and merge logic are unit-testable without any model calls.
- **Budget cap per goal.** Multi-agent runs burn tokens; a run has a token/cost ceiling
  and the lead stops dispatching when it's hit. Parallelize only when work genuinely
  partitions — an indivisible feature is one worker, not a team.
- Keep claims honest: if a metric isn't measured (merge success rate, escalation
  rate), label it unverified. The escalation rate on a REAL repo is the project's core
  benchmark — don't fake it on a toy repo and call it proven.
