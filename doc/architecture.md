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
  `Worker.run(task, worktree) -> WorkerResult` (`workers/base.py`), where the result
  is exactly one of a `diff` or a `scope_request` (globs it needs outside its
  file_scope — it stops rather than editing them, §5). v1 = Claude Code headless;
  alternates = AgentScope `ReActAgent`, SWE-agent. No runtime specifics leak into the
  engine.
- **Isolation = git worktrees** — one worktree + branch per in-flight task.
- **Safety = a container sandbox** — all agent-written code and test runs execute in a
  container, never the host. A diff is data until it passes review + merge; its tests
  are untrusted code. Behind one interface `Sandbox.run(cmd, workdir, timeout) ->
  RunResult` (`sandbox/base.py`); v1 = `DockerSandbox`, a fresh `docker run --rm` per
  command with only the work dir mounted, `--network none`, `--cap-drop ALL`,
  `no-new-privileges`, and memory/pid caps. Image: `sandbox/Dockerfile`
  (`seam-sandbox`: python + pytest + Claude Code). Test runs get `--network none`.
  Worker runs need the model API, so they run on the `--internal` network
  `seam-workers`, whose only reachable host is SEAM's credential proxy (§10). They
  get the proxy's URL and a *placeholder* credential; the real one is held only by
  the proxy. The worktree's `.git` pointer is overlaid read-only so a worker cannot
  repoint the host's git at a hostile config.
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
                      Rework (changes_requested / rework) = a NEW worker run on the
                      SAME branch + worktree, handed the review_notes.
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
**Budget cap** (CLAUDE.md conventions) is per goal and enforced by the lead — it stops
dispatching when the goal's token/cost ceiling is hit. It lives with goals, not tasks;
a `goals` table arrives with the coordinator.
The **architect** (owns the interface contract) is a hat the lead wears in v1; split
out later. Contract changes go through the lead, serialized — a worker never changes
a shared interface unilaterally (the main source of semantic conflicts).

**Orchestrator** (`engine/orchestrator.py`) — the loop that runs a team. One process,
and the *only* writer of task state: worker and review runs execute concurrently in
a thread pool and hand back results; the loop thread applies every transition (the
condition that keeps sqlite valid). Each pass: drain the merge queue (serially),
retry denied scope requests that merges may have freed, dispatch idle workers (rework
on their own in_progress tasks first, then a fresh claim → new worktree + branch
`seam/task-<id>` off the target), start one review if the reviewer is free; then wait
for any run to finish. A worker's changes are committed on its branch by the
orchestrator, and the diff stored at submit is git's (`target...branch`), not the
worker's. It stops when nothing is running and nothing can start (done, escalated,
blocked, or budget spent). The lead's budget is checked before every new agent run;
runs already in flight may overshoot it by at most one run each.
Denied scope requests are remembered in memory only; after a restart a blocked task
waits for the lead or a human.

**Review gate** (`reviewer/gate.py`) — deterministic checks before any model is paid:
reviewer ≠ author, non-empty diff, every changed path inside file_scope, the task's
own `test_cmd` green on the branch in the sandbox; only then the `Reviewer` runtime
judges the git diff against the spec, given the changed files as they were at the merge
base (smallest first, up to `context_chars`, default 200k characters; larger or binary
files are listed as omitted), so it checks the diff against the real surrounding code
rather than its memory of similar code. Files are sent whole by default; `run
--review-context focused` instead sends files over 12k characters as a focused view
(`reviewer/context.py`: an `ast` outline of every definition and assignment, plus the
full source of what the diff touches, uses, or aliases), ~3x cheaper but less tested
(§8). v1 reviewer runtime (`reviewer/claude_code.py`): `claude -p` with
**all tools disabled** and a JSON verdict schema, the diff on stdin, from an empty temp
dir (no project config). With no tools it can only read, so it runs outside the
sandbox; the diff is labelled untrusted in the prompt, and a manipulated approve
still has to pass the merge-time full suite.

---

## 3. Task state machine

```
open ─claim(worker)─▶ claimed ─start─▶ in_progress ─submit─▶ submitted
in_progress ─needs files outside scope─▶ scope_requested ─grant(lead)─▶ in_progress
                                                        ─deny(lead)──▶ blocked
blocked ─unblock(lead re-grants once disjoint)─▶ in_progress
submitted ─pickup(reviewer)─▶ in_review
in_review ─approve─▶ approved ─auto─▶ queued_for_merge
in_review ─changes_requested─▶ in_progress            (same branch, new run; bounded)
in_progress ─fail(worker run crashed/empty)─▶ in_progress  (re-run; bounded)
in_review ─fail(review run crashed)─▶ submitted            (re-review; bounded)
queued_for_merge ─take(merge engine, SINGLE SLOT)─▶ merging
merging ─clean─▶ merged                               (→ wake lead)
merging ─textual or semantic conflict─▶ needs_human
needs_human ─resolve(human/architect)─▶ queued_for_merge   (branch is fine as-is)
needs_human ─rework(human)─▶ in_progress                 (attempts reset to 0)
(attempts exceeded on review/fail/scope request) ─▶ needs_human
any non-terminal except merging ─cancel(lead)─▶ cancelled   (cascades to dependents)
on restart: task in `merging` ─merge commit on main─▶ merged
                               ─otherwise────────────▶ queued_for_merge
```

Implemented as a pure transition table in `engine/states.py` (`next_state`); the store
routes every state change through it. `cancel` is refused while `merging` — a merge in
flight finishes or is recovered, never abandoned half-applied. Cancelling a task
cancels every task that transitively lists it in `blocked_by`: they could never be
claimed, and would otherwise sit `open` forever. `rework` resets the retry budget
because a human's judgment has replaced the loop that exhausted it; `resolve` is for a
branch that needs no further edits (e.g. the human fixed main), `rework` for one that
does (e.g. escalated after failing review).

**Retry bound.** `attempts` bounds the review loop, failed worker runs (`fail`), and
scope requests (`request_scope`: each grant buys another worker run, so a task that
keeps widening its scope escalates rather than growing it until the budget runs out).
All three share one per-task count. Merge failures never loop:
the first textual or semantic conflict goes to `needs_human` (`merge_conflict` still
increments `attempts`, as a record of how often the task bounced).

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
id, goal_id, type(backend|frontend|devops|ui|docs),   (worker types; the reviewer
                                                       is a role, not a task type)
description, spec_ref (frozen contract section implemented),
file_scope[]  (module/dir-level globs this task may touch — see §5 on granularity),
blocked_by[]  (task ids that must be merged first), priority,
state, owner, branch, worktree_path, test_cmd,
attempts (review/merge bounces so far), review_notes, diff,
base_sha, merge_sha  (main before the merge; the merge commit — recorded before main
                      moves, cleared on each new `take`; see §6 crash-safety)
```

**Commit identity.** SEAM makes every commit (agents never run git), all under one
identity, `SEAM merge engine`. Who did what is in the message trailers: task commits
carry `SEAM-Task`, `SEAM-Worker` and `SEAM-Attempt`; merge commits carry `SEAM-Task`
and `SEAM-Worker` (the task's author). Separate them with
`git log --grep 'SEAM-Worker: backend-2'`.

**Run accounting** (`runs` table, `engine/usage.py`): one row per agent run against a
task: `role` (worker | reviewer | gate — a review the deterministic checks decided,
no model), `agent_id`, `outcome`, `cost_usd`, tokens split into uncached input,
cache reads, cache writes, and output, as the runtime reports them (Claude Code's
`modelUsage`, summed over every model a run used), and a review's `notes`, so a
rejection's reasons survive after a later approval overwrites the task's
`review_notes`. Older state dirs gain the column on open. It is accounting only: no state
transition reads it. `run` prints totals by role, `status` too, `inspect` per run.

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

**Tests and shared directories.** A task's tests count against its file_scope like any
other file. Put them inside the module's scope (`auth/tests/**`) or a per-module test
dir (`tests/auth/**`); a scope of plain `tests/**` overlaps every other task that adds
tests and serializes them all. Files that running code generates (`__pycache__/`,
`*.pyc`, `.pytest_cache/`; `ARTIFACT_EXCLUDES` in `engine/git.py`) are never staged, so
a worker that runs its tests does not trip the scope check with them.

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

**Merge gate** (single slot, serialized, crash-safe; `engine/merge.py`): for the one
task in `merging` (next taken = priority desc, id asc) —
1. trial-merge the branch onto current main with **no working tree**:
   `git merge-tree --write-tree`, then `commit-tree` → a two-parent merge commit
   that no ref points at yet.
2. textual conflict → `needs_human`, always (v1). Disjoint file_scopes make one
   impossible between SEAM tasks, so a textual conflict means main changed outside
   SEAM — a human's call, not an auto-resolver's.
3. **run the FULL test suite on the MERGED tree** in the sandbox (integration check —
   distinct from the reviewer's branch-level run). The tree is `git archive`d into a
   temp dir — a plain copy with no `.git`, so the sandboxed suite can't reach the repo.
4. green → publish (below), mark merged, wake lead; red or timed out → `needs_human`
   with the suite's output tail in `review_notes`.
5. release slot; the next queued task is merged against the new main.
If main's compare-and-swap fails (main moved outside SEAM mid-suite), nothing was
published and the task is requeued to merge against the new main. Any unexpected error
before publishing also requeues (main untouched) and re-raises. All engine git calls
run with hooks disabled, so no repository-supplied code executes on the host.

**The target branch must not be checked out** in any working tree: moving a checked-out
branch's ref leaves that tree's files stale. The engine refuses (`TargetCheckedOut`);
run SEAM against a dedicated clone of the real repo.
Crash-safety: the merge commit is built off main on a throwaway ref and the suite runs
on it there; main is never touched until both gates have passed. Publishing is then:
(a) record `base_sha`/`merge_sha` on the task, (b) compare-and-swap main from
`base_sha` to `merge_sha` (`git update-ref`), (c) mark `merged`. A crash can leave the
task `merging` at any point; on restart (`TaskStore.recover_merging`):
- `merge_sha` is on main → the crash fell between (b) and (c). Main only moved after
  the full suite passed, so both gates were met: finish it as `merged`. Rolling it
  back would discard a verified merge and move main backwards.
- otherwise → main was never moved: requeue to `queued_for_merge`.
Either way a partial merge never reaches main.

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

**Status.** Engine-level regression tests for both cases pass with scripted workers
and an inspect-only fake sandbox (`tests/test_orchestrator.py`, `test_v1_case*`). The
real-runtime run (Claude Code workers in Docker, Claude Code reviewer) is
`examples/greeter`. The greeter repo is a toy: passing it proves the mechanism, not the
escalation rate.

**Real-runtime results (2026-10-30, greeter, `--workers 1`, default model, OAuth auth;
cost is Claude Code's reported `total_cost_usd`, single runs, not a benchmark):**

| case | outcome | attempts | reported cost |
|---|---|---|---|
| 1 happy path | both tasks `merged`, zero human touch; main suite green (11 passed) | 0 rejections | $0.08 |
| 1, `--workers 2` | both `merged` by different workers (backend-1, backend-2), zero touch; main suite green (10 passed) | 0 rejections | $0.08 |
| 2 escalation | rename `merged`; `farewell` → `needs_human` (merge-time suite: `cannot import name 'greet'`); main never broken (2 passed) | rename rejected once (bytecode, see below) | $0.21 |

Both v1 success criteria were met once each. In the `--workers 2` run the two task
commits landed 4 s apart, about 12 s after start, so the runs overlapped (no start
times are logged; this is inferred from commit times). Not yet shown: any
repeat-rate or escalation-rate figure. The first case-1 attempt exposed a bug:
`git add -A` staged `__pycache__/` written by the worker's own test runs, which the
gate rejected as out-of-scope edits; fixed by `ARTIFACT_EXCLUDES` (§5). Branches
committed before that fix cannot drop the bytecode (deletions are excluded too) and
need a fresh run.

The v1 lead is a hand-written plan (`coordinator/lead.py`); its budget is per `run`
invocation, in memory. A goal loads once per store: a second load of the same goal id
raises `GoalAlreadyLoaded` rather than duplicating tasks (duplicates would hold the
same file_scopes and could never be claimed).

---

**Token use (2026-10-30, case 1, `--workers 2`, warm cache, single runs).** Measured
with the `runs` table:

| | before | after |
|---|---|---|
| worker input tokens, 2 runs | 85.6k (99% cache reads) | 47.0k (98%) |
| worker cost | $0.024 | $0.020 |
| reviewer cost, 2 reviews (Opus 5.5) | $0.059 | $0.027 |
| total | $0.08 | $0.05 |

Changes: workers get only `Bash, Edit, Glob, Grep, Read, Write` (no web, sub-agent or
notebook tools, whose definitions were resent on every request); the reviewer's rules
became its whole system prompt, with host settings, plugins and MCP not loaded. The
first run after either change pays a one-time cache write (that run cost $0.11). The
reviewer still approved a correct diff and rejected a planted `.upper()`→`.lower()`
bug. Caveman was evaluated and not adopted: its output-voice saving is small for
code-heavy runs, its log compression targets large outputs the greeter lacks, and
its cloud features would send model traffic to a third party past the proxy.

**First real-codebase run: boltons-bench-1 (2026-10-30, `examples/boltons`).**
boltons @ `d31d821` (775 tests incl. doctests, offline in ~8 s); 8 feature tasks, one
per module, written by the AI that built SEAM (so **not an independent benchmark**),
`--workers 2`, full suite with doctests at merge.

| result | value |
|---|---|
| merged / needs_human | **8 / 0** (escalation rate 0/8) |
| review rejections, failed runs | 0, 0 |
| master after the run | 807 passed (775 + 32 new), 16 commits, +360 lines |
| cost | $0.67: workers $0.353 (8 runs, 89% cache hit), reviewer $0.312 (8 reviews) |

What this does and doesn't show: the mechanism ran end to end on real code with no
human touch. It does **not** exercise escalation on real code: one task per module
with disjoint scopes gave the merge suite nothing to catch, so 0/8 says little about
the escalation rate a real goal would see. Two findings:
- *The reviewer sees only the diff.* On `LRI.resize` it noted it could not open
  `cacheutils.py` and judged eviction accounting partly from memory of upstream
  boltons. It was right here, but on unfamiliar code it would be guessing. Candidate
  fix: give it the pre-change contents of the changed files (bounded), still with no
  tools.
- *Review cost ~= worker cost.* Opus 5.5 reviews wrote ~1.2k output tokens each, much
  of it in approval notes nobody acts on.

Follow-up (same day): the gate now passes the changed files' original text (§2).
Re-reviewing the `LRI.resize` branch with it, the reviewer checked the change against
`cacheutils.py` itself instead of memory. Cost of that one review (21k input tokens,
Opus 5.5): $0.19 with Claude Code's prompt caching (all cache writes, never reused),
$0.10 with caching disabled for reviews, $0.09 with one-sentence approval notes
(output 864 → 428 tokens); before the change, diff-only reviews cost ~$0.04. The
planted `.upper()`→`.lower()` bug was still rejected. Single measurements.

**boltons-bench-2: one goal with dependencies (2026-10-30, `examples/boltons/plan2.json`).**
Goal: query-parameter editing for `URL`. 5 tasks: `OrderedMultiDict.setlist` (frozen
contract) in `dictutils`; the same on urlutils' vendored OMD copy; `URL.replace_query`
(blocked by `setlist`, since `QueryParamDict` subclasses `dictutils.OMD` in-package);
`URL.without_query_params`; docs (blocked by the three it documents). Three tasks share
`urlutils.py`. Same authorship caveat as bench-1; every dependency the author could
see was declared.

| result | value |
|---|---|
| merged / needs_human | **5 / 0** |
| review rejections | 2, both fixed on the next run (see below) |
| ordering | `setlist` ∥ vendored copy; `without_query_params` claimed only after the vendored copy merged (shared scope); `replace_query` only after `setlist` and `without_query_params` merged; docs last |
| master | 786 passed |
| cost | $1.35: workers $0.37 (7 runs), reviewer **$0.98** (7 reviews) |

The two rejections were real (both confirmed later by running the buggy versions in
the sandbox): `setlist` matched keys by equality only, so a `nan` key was duplicated
instead of replaced (fixed to identity-then-equality, as `dict` does);
`without_query_params` assigned `ret.query_params`, a `cachedproperty`, leaving the
URL's `_query` text stale — a defect visible only from the original `urlutils.py`,
which the reviewer now receives. (Those notes were reconstructed from the diffs:
rejection notes were overwritten on approval until runs started keeping them.) The
proxy refused one `POST /v1/messages/count_tokens`; it is now allowed.

Read honestly: claim serialization, dependency ordering and the review loop all
worked on real code, and no task escalated. The reviewer is now ~73% of cost:
`urlutils.py` alone is ~55k characters, so each review read ~35k tokens on Opus.
Escalation on real code is still unobserved; it needs a goal whose decomposition is
wrong somewhere, which a careful hand-written plan rarely is.

**Focused reviewer context, measured (2026-10-30).** Re-reviews of bench-2's two
buggy first versions, both confirmed by running them in the sandbox: buggy
`without_query_params` returned a URL whose `to_text()` was `?b=2` while `qp` and
`query_params` still held `a=1, a=3` (worker tests passed); buggy `setlist` duplicated
a `nan` key instead of replacing it. Per review, on Opus 5.5:

| context | `without_query_params` bug caught | `setlist` bug caught | cost / review |
|---|---|---|---|
| whole files (default) | 1 of 2 | 1 of 2 | $0.13–0.18 |
| focused view | 0 of 3 | 0 of 1 | $0.04–0.06 |

(Whole-file counts include the original run's rejections.) Too few samples to rank
the two, but the direction is against the focused view, so it stays opt-in. The
larger finding is that **an LLM review is a probabilistic gate**: a significant,
test-passing bug got through whole-file review half the time. The merge-time suite
cannot catch what the tests don't assert; making review reliable on such bugs (e.g.
several independent reviews, any rejection wins, or a reviewer that proposes a
failing test) is open work.

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

---

## 10. Credential proxy for worker containers

**Constraint served: safety (invariants 10 and 13).** Before this, a worker container
held the real API credential in its environment *and* had open network egress. The
worker runs an agent over untrusted repo content, so a prompt-injected worker can read
the credential and send it, or the repo, anywhere. For OAuth that credential is a
long-lived subscription token. This is the main gap before SEAM runs on a real repo.

**Design.**
```
worker container ──(internal net, no egress)──▶ seam-proxy ──(bridge)──▶ api.anthropic.com
  dummy credential                               real credential (-e NAME)
  ANTHROPIC_BASE_URL=http://seam-proxy:8080      swaps the auth header; allowlist
```
- `docker network create --internal seam-workers`: containers on it reach each other
  but nothing outside. Worker runs move from `bridge` to this network.
- `seam-proxy`: one long-lived container per `run`, on `seam-workers` *and* `bridge`,
  started by the CLI before any worker and removed after (`docker rm -f`, also on
  Ctrl-C). It is SEAM's own code (stdlib Python, in the `seam-sandbox` image), not
  agent-written, and it is the only process that holds the credential.
- Forwarding rule: only `POST /v1/messages` and `POST /v1/messages/count_tokens` (any
  query string) are forwarded to `https://api.anthropic.com`; everything else gets
  403. (`count_tokens` was added after a real run showed Claude Code calling it.) The proxy replaces
  `Authorization: Bearer …` (OAuth) or `x-api-key` (API key) with the real value,
  passes all other headers through, and streams the response unbuffered (SSE).
- Workers get a dummy credential *of the same kind* as the real one, so Claude Code
  uses the matching auth mode and headers, plus
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.

**Built** in `sandbox/proxy.py` (the proxy) and `CredentialProxyContainer` in
`sandbox/docker.py` (network + container lifecycle); `cli.py run` starts it, then
checks from the worker network that the proxy answers and that `1.1.1.1:443` is
unreachable, before any agent runs. Exactly one credential env var may be set. The
proxy serves only clients from the internal network's subnet, and its log (request
lines and refusals, never headers) is kept when it stops; `run` prints any refusals.

**Evidence so far (2026-10-30, Claude Code 2.1.312).** A probe with
`ANTHROPIC_BASE_URL` pointing at a logging stub on an `--internal` network and a dummy
`CLAUDE_CODE_OAUTH_TOKEN` showed that Claude Code sends only `POST /v1/messages?beta=true`
with `Authorization: Bearer <dummy>` and the `oauth-2025-04-20` beta header, and
attempts nothing else. The stub returned 401, so no request reached Anthropic.
**End to end (2026-10-30):** greeter case 1 with `--workers 2` through the proxy:
both tasks `merged`, zero touch, main suite green (10 passed), no requests refused,
proxy container removed afterwards. Reported cost $0.22, against $0.08 for the same
case without the proxy. With per-run token recording, later runs through the proxy
show a 98–99% prompt-cache hit rate for workers, so the proxy does not defeat
caching; the likely cause of the $0.22 is a one-time cache write, since any change to
the worker's prompt prefix (here the env) makes the first run re-cache it, as seen
again after the tool change below. A worker task that needs other endpoints (web fetch, MCP)
would now be refused; none of the greeter tasks did.

**What it fixes.** The credential never enters a worker container. Workers lose all
egress except the model API, so they can't exfiltrate the repo to third parties or
download dependencies at run time (the image must carry them).

**What it does not fix.** A compromised worker can still make arbitrary model calls
through the proxy, spending quota until its run is killed (bounded by the run timeout
and `--max-budget-usd`, which the worker itself enforces, so not trusted). A later
step can have the proxy meter tokens per run and cut off at a cap. Data sent to the
model API still leaves the machine, as it does today. The reviewer runs on the host
with the host login and is unaffected.

**Rejected alternatives.**
- *Proxy on the host*: an `--internal` network can't reach the host, and any network
  that can also has open egress.
- *Scoped short-lived tokens*: `claude setup-token` issues one long-lived token;
  there is no per-run scoped credential to mint.
- *Egress firewall only*, keeping the real token in the container: stops sending it
  elsewhere, but the token still sits in an environment the agent can read.

The isolation itself is tested against real containers
(`test_real_proxy_reachable_only_from_the_internal_network`: health answers, other
endpoints get 403, no route to the internet).

---

## 11. Adversarial probe tests in review (built; opt-in, `run --review-probes`: measured 4/4 catch, 3/4 false rejection)

**Constraint served: review reliability (invariants 5, 6).** §8 measured that a single
LLM review let a significant, test-passing bug through half the time: judgment alone
is a probabilistic gate.

**Design.** The reviewer stays tool-less. Its structured verdict gains `probes`: 1–3
small pytest files targeting the riskiest behavior the spec implies (aliases,
mutation, edge values, consistency between related APIs), written *even when it would
approve*. The gate exports the branch, adds the probes under `seam_probes/`, and runs
them in the sandbox (no network):
- any probe fails → reject; the notes carry the failing test and its output, a
  reproduction rather than an opinion;
- all pass → the reviewer's verdict stands.

Probes are never merged; they are review evidence, kept with the run. Python/pytest
only at first (`probe_cmd` on the gate).

**Built** in `reviewer/claude_code.py` (`PROBE_SCHEMA`, `PROBE_INSTRUCTIONS`; at most 3
probes, names sanitized to `test_probe_*.py`) and `ReviewGate.run_probes`. A failing
probe ALWAYS rejects (owner's decision): pytest exit 1 (a test failed) or 2 (collection
failed, e.g. importing what the change never added). Exit 5 / usage errors / timeouts
mean broken probes and leave the verdict; probes that are not valid Python are
dropped unrun (parsed with `ast`, never executed on the host). Probes are stored in
`runs.probes`. Mechanism check without a model: a hand-written alias probe rejected
bench-2's buggy `without_query_params` and passed the fixed one, in the real sandbox.
Whether the *reviewer* writes such probes is step 3 below.

**Measured (2026-10-30, bench-2, whole-file context, Opus 5.5, 2 reviews per version,
$1.48 total, $0.15–0.27 per review):**

| version | rejected | right? |
|---|---|---|
| buggy `setlist` (`nan` key) | 2/2, by probe | caught |
| buggy `without_query_params` (stale `qp`) | 2/2, by probe | caught |
| fixed `setlist` | 2/2, by probe | **false rejection** |
| fixed `without_query_params` | 1/2, by probe | **false rejection** |

Catch rate 4/4 (single reviews without probes: 1/3), but false rejections 3/4. All
three false rejections had the same cause, confirmed by running the probe scenario
on the *original* code in the sandbox: the probe asserted behavior the original
boltons does not have — `FastIterOrderedMultiDict.items()` already disagrees with
`omd[k]` for repeated keys, and edits made through `url.qp` are already lost from
`to_text()`. (Both look like genuine upstream quirks; neither is the task's fault.)

**Decision: stays opt-in.** Under always-reject, a 3/4 false-rejection rate would burn
every correct task's attempts and escalate it. The open problem is telling "the
change broke this" from "this was never true"; running probes against the original
code does not settle it, since a probe of new API fails there before reaching its
faulty assertion. Candidates: return a failing probe and the original code to the
reviewer for one adjudication call before rejecting (changes the always-reject
policy, so the owner's call), or require each probe to name the spec clause or
original-code behavior it asserts.

---

## 12. The AI lead (LLM planner) — built, not yet measured

**Constraint served: invariant 7, and an honest benchmark.** The v1 lead executes a
hand-written plan, so every escalation rate so far measures its author's planning.
An LLM planner writes the plan from a goal in prose; its plans, run unedited, are
what the escalation rate should be measured on. (CLAUDE.md deferred this role until
both v1 cases passed; they have, §8.)

**Design.**
- *Same output.* The planner emits exactly the plan format `load_plan` accepts (goal;
  tasks with key, type, description, spec_ref, file_scope, blocked_by, test_cmd) and
  goes through the same validation (unique keys, known deps, no cycles). Nothing
  downstream changes.
- *Behind an interface.* `Planner.plan(goal, repo_snapshot) -> PlanDraft` in
  `coordinator/` (plain Python); the Claude Code adapter lives in a new `planners/`
  package (invariants 7, 8: no runtime imports in the coordinator).
- *Sandboxed, through the credential proxy.* The planner must read the repo, and repo
  content can carry prompt injection; on the host its Read tool could reach any file
  (e.g. `~/.ssh`) and smuggle it into task text. So it runs like a worker: in the
  sandbox on the `seam-workers` network, over a `git archive` export (no `.git`),
  with only Read/Glob/Grep, returning the plan as `--json-schema` structured output.
- *Human checkpoint first.* `cli.py plan --repo R --goal "…" --out plan.json` writes the
  plan for a human to read or edit before `run --plan`. Plan-and-run in one step comes
  only after LLM plans are measured.
- *Planning rules given to the model:* module/directory-level scopes with the tests
  inside them; `blocked_by` for every task that uses another task's new API; a frozen
  contract in `spec_ref` for any shared-interface change; an indivisible feature is one
  task; report scopes that overlap (they will serialize).
- *Cost* is charged to the goal's budget.

**Built:** `coordinator/planner.py` (`Planner`, `PlanDraft`, `PlannerFailed`),
`coordinator/lead.py:plan_from_dict` (the shared validation), `planners/claude_code.py`
(`ClaudeCodePlanner`: tools Read/Glob/Grep, `PLAN_SCHEMA` structured output), and
`cli.py plan`, which exports the target to the state dir, starts the credential proxy,
runs the planner, and writes the plan JSON with the goal text and the planner's notes
(`goal_text`, `planner_notes`, ignored by `load_plan`). The `plan` command has its own
`--budget-usd` cap; budgets are per invocation (§8), so planning cost is reported, not
carried into `run`.

**Measurement.** Give the planner the bench-1 and bench-2 goals in prose; compare its
plans with the hand-written ones (tasks, scopes, deps); then run its plans unedited
and record merged / needs_human — the first escalation rate not produced by the
author's own planning.
