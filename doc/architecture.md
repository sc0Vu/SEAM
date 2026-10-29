# Architecture — SEAM (Software Engineering Agent Merge)

> Living design doc. Source of truth for the architecture. CLAUDE.md is the
> always-loaded constitution; this file is the detail, read before working on the
> engine, state machine, merge/review protocol, or task schema. Keep it in sync with
> the code in the same change (per CLAUDE.md Definition of Done).

**SEAM** is a multi-agent system where one coordinator decomposes a goal into tasks
and many specialized agents implement them **in parallel on one real repository** —
each in its own git worktree — while an independent reviewer and a **serialized
merge engine** integrate their work. The novel component is the autonomous **review +
merge engine**; everything else is commodity we build on.

Design lineage: the coordination pattern (one planner, many stateless workers pulling
from a shared task queue, datastore-as-memory, proof-before-done) is borrowed from
autonomous pentesting agents (ARTEX), retargeted from pentesting — where workers
produce *append-only facts* that never conflict — to code, where workers edit
*mutable shared state* and conflicts are the central problem. So the whole design is
organized around *preventing* conflicts cheaply and *resolving* the rare residual
ones safely.

---

## 1. Substrate decision — own the engine, rent the workers

The parts you build *on* are commodities; the one thing nobody has built is the
autonomous integration engine. So: **own the coordinator + merge engine; treat the
worker runtime as pluggable.**

- **Worker runtime = pluggable.** A worker is any headless coding agent that, given a
  task + a git worktree, returns a diff. Behind one interface
  `Worker.run(task, worktree) -> diff`. v1 = Claude Code headless; alternates =
  AgentScope `ReActAgent`, SWE-agent. No runtime specifics leak into the engine.
- **Isolation = git worktrees** — one worktree + branch per in-flight task.
- **Safety = a container sandbox** — all agent-written code and test runs execute in a
  container, never the host. A diff is data until it passes review + merge; its tests
  are untrusted code.
- **Product = coordinator + merge/review engine** — the novel IP.

Rejected as *the architecture*: Claude Code agent teams and AgentScope — fine as
worker runtimes, but neither does autonomous merge, so building the novel component
inside them couples the IP to someone else's frame.

---

## 2. Components

```
LEAD/PLANNER (one)  → decomposes goal into typed, file-scoped, dependency-ordered
                      tasks; sole task producer; owns the frozen interface contract;
                      grants scope-expansion requests; enforces the budget cap.
      │ writes tasks
      ▼
TASK STORE  → shared truth + state machine (sqlite v1 → postgres).
      │ atomic claim (typed + file-scope-disjoint)
      ▼
WORKERS (N, typed)  → each claims one task, works in its own worktree/branch inside
                      the sandbox, returns a diff, marks submitted. Stateless, one
                      task, then stop. Requests scope expansion rather than escaping scope.
      ▼
REVIEWER (independent)  → runs the task's tests on the branch + reviews diff vs spec.
                          approve | changes_requested.
      ▼
MERGE ENGINE (novel, SERIALIZED)  → rebase → FULL suite on merged tree → merge | escalate.
      │ merged → graph changed → wake lead (re-plan)
```

ARTEX mapping: lead = planner (sole producer, singular), workers = ARTEX workers,
task store = dual graph (merged main = asset/truth; per-task branch =
exploration/working), reviewer + merge = prove_goal gate, made real for mutable code.
The **architect** (owns the interface contract) is a hat the lead wears in v1; split
out later. Contract changes go through the lead, serialized — a worker never changes
a shared interface unilaterally (the main source of semantic conflicts).

---

## 3. Task state machine

```
open ─claim(worker)─▶ claimed ─start─▶ in_progress ─submit─▶ submitted
in_progress ─needs files outside scope─▶ scope_requested ─grant(lead)─▶ in_progress
                                                        ─deny(lead)──▶ blocked
submitted ─pickup(reviewer)─▶ in_review
in_review ─approve─▶ approved ─auto─▶ queued_for_merge
in_review ─changes_requested─▶ in_progress            (same worker/branch; bounded)
queued_for_merge ─take(merge engine, SINGLE SLOT)─▶ merging
merging ─clean─▶ merged                               (→ wake lead)
merging ─textual or semantic conflict─▶ needs_human
needs_human ─resolve(human/architect)─▶ queued_for_merge
(attempts exceeded on review/merge) ─▶ needs_human
any non-terminal except merging ─cancel(lead)─▶ cancelled
on restart: any task in `merging` ─rollback+requeue─▶ queued_for_merge
```

Implemented as a pure transition table in `engine/states.py` (`next_state`); the store
routes every state change through it. `cancel` is refused while `merging` — a merge in
flight finishes or is recovered, never abandoned half-applied.

**Scope-holding states.** A task's file_scope stays reserved (§5) in every state
except `open`, `merged`, and `cancelled` — i.e. whenever a branch with live edits
exists. This deliberately includes `submitted`, `approved`, `blocked`, and
`needs_human`: those branches may still merge, so a second task touching the same
files would reintroduce exactly the conflict the claim exists to prevent.

Invariants: `merging` is a single global slot; no `merged` without `in_review` +
merge-time full suite; `changes_requested` returns to the same worker and is **bounded
(default 3 attempts)** → `needs_human`; a crash during `merging` rolls the branch back
out of main and requeues (partial merge never persists).

---

## 4. Task schema
```
id, goal_id, type(backend|frontend|devops|ui|docs|review),
description, spec_ref (frozen contract section implemented),
file_scope[]  (module/dir-level globs this task may touch — see §5 on granularity),
blocked_by[]  (task ids that must be merged first), priority,
state, owner, branch, worktree_path, test_cmd,
attempts (review/merge bounces so far), review_notes, diff
```

---

## 5. Task assignment — atomic, typed, file-scope-disjoint

```sql
UPDATE tasks SET state='claimed', owner=$worker
WHERE id = (
  SELECT t.id FROM tasks t
  WHERE t.state='open' AND t.type=$worker_type
    AND NOT EXISTS (SELECT 1 FROM unnest(t.blocked_by) b
                    JOIN tasks p ON p.id=b WHERE p.state <> 'merged')
    AND NOT EXISTS (SELECT 1 FROM tasks x
                    WHERE x.state IN ('claimed','in_progress','scope_requested',
                                      'submitted','in_review','approved','blocked',
                                      'queued_for_merge','merging','needs_human')
                      AND overlaps(x.file_scope, t.file_scope))  -- glob test, below
  ORDER BY t.priority DESC, t.id ASC
  LIMIT 1 FOR UPDATE SKIP LOCKED
) RETURNING *;
```
`FOR UPDATE SKIP LOCKED` + MVCC → Postgres once workers write directly. The
file_scope non-overlap check is the conflict-prevention core: two in-flight workers
never touch the same files, so most merges are trivial. (v1 sqlite is valid only while
a single orchestrator process is the sole writer.)

**v1 implementation** (`store/sqlite.py`): the SQL above is the Postgres target. In
sqlite the claim is a read-check-write inside one `BEGIN IMMEDIATE` transaction, with
the scope-holding set from §3 and the glob overlap
test below. The single `merging` slot is also enforced by the database itself: a
partial unique index on `state WHERE state='merging'` rejects a second `take`.

**Scope granularity + the chicken-and-egg.** The lead can't perfectly predict exact
files for a new feature, so **file_scope is declared at module/directory granularity**
(globs like `auth/**`, `api/users.*`), not exact file lists, and overlap is tested on
those globs. Array `&&` in the SQL above only matches *identical* strings (`auth/**`
vs `auth/login.py` would pass as disjoint), so overlap is a real glob test
(`engine/scope.py`): segment-wise, `**` overlaps anything below it, a wildcard segment
is matched against a literal one, and two wildcard segments are assumed to meet. It is
**conservative** — false "overlap" only serializes two tasks; a false "disjoint" would
break invariant 2, and it never reports one. When a worker discovers mid-task that it must touch something outside its
scope, it transitions `in_progress → scope_requested` with the paths; the lead
re-checks disjointness against all in-flight tasks and either grants (expands
file_scope) or denies (→ `blocked`, re-planned later). This keeps invariant 2 true
even though scopes are predicted, not known.

---

## 6. The merge + review engine (novel component)

Two gates. Review runs in parallel across tasks; **merge is strictly serial.** They
run **two different test passes** on purpose:

**Review gate** (independent, parallelizable): a reviewer agent in a different context
from the author gets the diff + `spec_ref` + results of **the task's own tests run on
its branch**, and verifies (1) those tests pass (it runs them in the sandbox, doesn't
trust the worker), (2) the diff implements the spec, not something adjacent, (3) no
correctness/security red flags, (4) stayed in file_scope. → approve | changes_requested
(bounded; exceed → needs_human).

**Merge gate** (single slot, serialized, crash-safe): for the one task in `merging` —
1. checkout main; merge/rebase the branch.
2. textual conflict → trivial auto-resolve, else `needs_human`.
3. **run the FULL test suite on the MERGED tree** in the sandbox (integration check —
   distinct from the reviewer's branch-level run).
4. green → commit merge, mark merged, wake lead; red → `needs_human`.
5. release slot; next queued task rebases onto the new main.
Crash-safety: the branch is merged into a throwaway ref / the merge is only committed
to main as the last step, so a crash mid-step leaves main untouched and the task is
rolled back to `queued_for_merge` on restart.

**Semantic-conflict escalation** (the honest boundary — not solved, by design): two
branches pass their own tests, no textual conflict, but break merged. Detected by step
3 failing with no textual conflict → `needs_human`/architect. **Escalation rate is the
project's core metric.**

**Why escalation stays rare**: (1) file_scope non-overlap, (2) frozen lead-owned
contract kills the interface-conflict class, (3) one-logical-change tasks → small clean
diffs. Frequent escalation = upstream decomposition/contract fault, not the merger.

**Known throughput ceiling**: the single merge slot serializes integration, so merge
throughput is bounded regardless of worker count. This is intended (correctness over
speed); workers still work in parallel, only integration is serial. If the merge slot
becomes the bottleneck, the fix is faster test suites / smaller diffs, not parallel
merges.

---

## 7. Reuse (don't rebuild)
git worktrees (Claude Code teams / Conductor); container sandbox (AgentScope's model,
or plain Docker) for untrusted execution; SWE-agent `git diff` as the worker
deliverable; Aider tree-sitter repo map to compute file_scope + worker context; Agyn's
reviewer-approves-PR gate; MetaGPT's structured-doc blackboard for the contract;
Claude Code headless / AgentScope as worker runtime.

---

## 8. v1 walking skeleton
1 lead (hardcoded task list ok) + 2 workers + 1 reviewer + merge engine, in a sandbox.
Two cases on a real small repo: (1) two disjoint-scope tasks merge green autonomously;
(2) two shared-interface tasks ESCALATE (caught at merge-time full suite), never
silently merge a break. Success = (1) zero-touch, (2) correctly escalated. Only then
add worker types, Postgres, the architect role, the scope-expansion UX polish, and a UI.

---

## 9. Risks
- **Semantic merge is unsolved in general** — escalation is a feature; the lever is
  decomposition/contract quality, not a cleverer merger.
- **Reviewer strength gates everything** — a weak reviewer passes bad diffs or thrashes
  on good ones. Invest here.
- **Serialized merge is a throughput ceiling** (by design) — accept it; optimize test
  speed, not merge parallelism.
- **Cost scales with workers** (~3–4×+); the budget cap and "only parallelize genuinely
  partitionable work" are the controls.
- **Toy-repo success ≠ large-repo success** — the honest benchmark is escalation rate
  on a real codebase.
- **Predicted file_scope can be wrong** — the scope-expansion protocol (§5) is what
  keeps invariant 2 true when a prediction misses; watch how often it fires.
