"""SQLite task store (doc/architecture.md §4-5).

Valid ONLY while a single orchestrator process is the sole writer of task state.
Once worker processes write task state directly, move to Postgres with
`FOR UPDATE SKIP LOCKED` (CLAUDE.md, Tech).

Every write runs inside `BEGIN IMMEDIATE`, which takes SQLite's write lock up front, so
each read-check-write (the claim, the merge-slot take) is atomic.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from engine.errors import MergeSlotBusy, StaleState, TaskNotFound
from engine.scope import scopes_overlap
from engine.states import DEFAULT_MAX_ATTEMPTS, SCOPE_HOLDING, Event, State, next_state
from engine.task import Task, TaskSpec

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id       TEXT    NOT NULL,
    type          TEXT    NOT NULL,
    description   TEXT    NOT NULL,
    spec_ref      TEXT    NOT NULL DEFAULT '',
    file_scope    TEXT    NOT NULL,             -- JSON array of globs
    blocked_by    TEXT    NOT NULL DEFAULT '[]', -- JSON array of task ids
    priority      INTEGER NOT NULL DEFAULT 0,
    state         TEXT    NOT NULL DEFAULT 'open',
    owner         TEXT,
    branch        TEXT,
    worktree_path TEXT,
    test_cmd      TEXT    NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    review_notes  TEXT    NOT NULL DEFAULT '',
    diff          TEXT    NOT NULL DEFAULT '',
    base_sha      TEXT,                          -- main before this merge
    merge_sha     TEXT                           -- the merge commit, recorded pre-publish
);
-- Invariant 4 enforced by the database itself: at most one row may be `merging`.
CREATE UNIQUE INDEX IF NOT EXISTS one_merging ON tasks(state) WHERE state = 'merging';
"""

# Fields `transition` may set alongside a state change.
_UPDATABLE = frozenset({"owner", "branch", "worktree_path", "review_notes", "diff"})


class TaskStore:
    """Task store with an atomic, typed, file-scope-disjoint claim.

    Upholds: file-scope disjointness among scope-holding tasks (invariant 2), the
    single `merging` slot (invariant 4), and legal-only transitions with bounded
    retries (invariants 5, 11), by routing every state change through
    `engine.states.next_state` with a compare-and-swap on the current state.
    """

    def __init__(self, path: str | Path = ":memory:", max_attempts: int = DEFAULT_MAX_ATTEMPTS):
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self.max_attempts = max_attempts

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # -- reads ---------------------------------------------------------------

    def get(self, task_id: int) -> Task:
        row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return _to_task(row)

    def list(self, state: State | None = None) -> list[Task]:
        if state is None:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY id")
        else:
            rows = self._db.execute("SELECT * FROM tasks WHERE state = ? ORDER BY id", (state,))
        return [_to_task(r) for r in rows]

    # -- writes --------------------------------------------------------------

    def create(self, spec: TaskSpec) -> Task:
        """Insert a new `open` task. Only the lead calls this (invariant 7)."""
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO tasks (goal_id, type, description, spec_ref, file_scope,"
                " blocked_by, priority, test_cmd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spec.goal_id, spec.type, spec.description, spec.spec_ref,
                    json.dumps(list(spec.file_scope)), json.dumps(list(spec.blocked_by)),
                    spec.priority, spec.test_cmd,
                ),
            )
        return self.get(cur.lastrowid)

    def claim(self, worker_id: str, worker_type: str) -> Task | None:
        """Atomically claim the best open task for a worker, or return None.

        Eligible: state `open`, matching type, every `blocked_by` task `merged`, and a
        file_scope disjoint from every scope-holding task (invariant 2). Picks by
        priority desc, then id asc.
        """
        with self._tx() as db:
            rows = db.execute("SELECT * FROM tasks").fetchall()
            by_id = {r["id"]: r for r in rows}
            held = [json.loads(r["file_scope"]) for r in rows if r["state"] in SCOPE_HOLDING]
            candidates = sorted(
                (r for r in rows if r["state"] == State.OPEN and r["type"] == worker_type),
                key=lambda r: (-r["priority"], r["id"]),
            )
            for r in candidates:
                deps = json.loads(r["blocked_by"])
                if any(d not in by_id or by_id[d]["state"] != State.MERGED for d in deps):
                    continue
                scope = json.loads(r["file_scope"])
                if any(scopes_overlap(scope, h) for h in held):
                    continue
                db.execute(
                    "UPDATE tasks SET state = ?, owner = ? WHERE id = ? AND state = ?",
                    (State.CLAIMED, worker_id, r["id"], State.OPEN),
                )
                row = db.execute("SELECT * FROM tasks WHERE id = ?", (r["id"],)).fetchone()
                return _to_task(row)
        return None

    def transition(self, task_id: int, event: Event, **fields: str) -> Task:
        """Apply `event` to a task, optionally setting `fields`, atomically.

        The new state comes from `engine.states.next_state` (so illegal events raise
        `IllegalTransition`); the update is a compare-and-swap on the current state.
        Taking the merge slot while another task holds it raises `MergeSlotBusy`.
        Cancelling a task also cancels every task that (transitively) depends on it,
        since a dependency that will never merge would leave them `open` forever.
        """
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot set fields via transition: {sorted(unknown)}")
        with self._tx() as db:
            task = self.get(task_id)
            new_state, attempts = next_state(task.state, event, task.attempts, self.max_attempts)
            sets = ", ".join(f"{k} = ?" for k in ("state", "attempts", *fields))
            try:
                cur = db.execute(
                    f"UPDATE tasks SET {sets} WHERE id = ? AND state = ?",
                    (new_state, attempts, *fields.values(), task_id, task.state),
                )
            except sqlite3.IntegrityError as e:
                raise MergeSlotBusy(task_id) from e
            if cur.rowcount != 1:
                raise StaleState(task_id)
            if new_state is State.CANCELLED:
                _cascade_cancel(db, task_id)
            if event is Event.TAKE:  # a fresh merge attempt: forget any earlier one
                db.execute(
                    "UPDATE tasks SET base_sha = NULL, merge_sha = NULL WHERE id = ?",
                    (task_id,),
                )
        return self.get(task_id)

    def grant_scope(self, task_id: int, extra_globs: list[str]) -> Task:
        """Grant a scope-expansion request iff the expanded scope stays disjoint from
        every other scope-holding task (invariant 3).

        From `scope_requested`: grant -> `in_progress`, or deny -> `blocked`.
        From `blocked` (the lead retrying a denied request): grant -> `in_progress`
        (`unblock`), or leave the task `blocked` and unchanged if it still clashes.
        """
        with self._tx() as db:
            task = self.get(task_id)
            new_scope = [*task.spec.file_scope, *extra_globs]
            others = db.execute(
                "SELECT file_scope, state FROM tasks WHERE id != ?", (task_id,)
            ).fetchall()
            clash = any(
                scopes_overlap(new_scope, json.loads(o["file_scope"]))
                for o in others
                if o["state"] in SCOPE_HOLDING
            )
            if task.state is State.BLOCKED:
                if clash:
                    return task
                event = Event.UNBLOCK
            else:
                event = Event.DENY_SCOPE if clash else Event.GRANT_SCOPE
            new_state, attempts = next_state(task.state, event, task.attempts, self.max_attempts)
            scope = task.spec.file_scope if clash else new_scope
            cur = db.execute(
                "UPDATE tasks SET state = ?, file_scope = ? WHERE id = ? AND state = ?",
                (new_state, json.dumps(list(scope)), task_id, task.state),
            )
            if cur.rowcount != 1:
                raise StaleState(task_id)
        return self.get(task_id)

    def record_merge_commit(self, task_id: int, base_sha: str, merge_sha: str) -> Task:
        """Record the merge about to be published, BEFORE main is moved (invariant 12).

        The merge engine builds the merge commit off main, runs the full suite on it,
        calls this, and only then moves main from `base_sha` to `merge_sha`. So if a
        crash leaves the task `merging`, recovery can tell whether main moved.
        """
        with self._tx() as db:
            cur = db.execute(
                "UPDATE tasks SET base_sha = ?, merge_sha = ? WHERE id = ? AND state = ?",
                (base_sha, merge_sha, task_id, State.MERGING),
            )
            if cur.rowcount != 1:
                raise StaleState(task_id)
        return self.get(task_id)

    def recover_merging(self, landed: Callable[[str], bool]) -> list[Task]:
        """On restart, settle any task stuck in `merging` (invariant 12).

        `landed(sha)` answers whether a commit is on main (the caller owns git; the
        store does not). If the task's recorded merge commit landed, main only moved
        after the full suite passed, so both gates were met: finish it as `merged`.
        Otherwise main was never touched: requeue it to `queued_for_merge`.
        """
        settled = []
        for t in self.list(State.MERGING):
            done = t.merge_sha is not None and landed(t.merge_sha)
            settled.append(self.transition(t.id, Event.MERGE_CLEAN if done else Event.RECOVER))
        return settled


def _cascade_cancel(db: sqlite3.Connection, root_id: int) -> None:
    """Cancel every non-terminal task that transitively depends on `root_id`."""
    pending = [root_id]
    while pending:
        cancelled = pending.pop()
        rows = db.execute("SELECT id, state, blocked_by FROM tasks").fetchall()
        for r in rows:
            if cancelled not in json.loads(r["blocked_by"]):
                continue
            if r["state"] in (State.MERGED, State.CANCELLED, State.MERGING):
                continue
            db.execute("UPDATE tasks SET state = ? WHERE id = ?", (State.CANCELLED, r["id"]))
            pending.append(r["id"])


def _to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        spec=TaskSpec(
            goal_id=row["goal_id"],
            type=row["type"],
            description=row["description"],
            file_scope=tuple(json.loads(row["file_scope"])),
            spec_ref=row["spec_ref"],
            blocked_by=tuple(json.loads(row["blocked_by"])),
            priority=row["priority"],
            test_cmd=row["test_cmd"],
        ),
        state=State(row["state"]),
        attempts=row["attempts"],
        owner=row["owner"],
        branch=row["branch"],
        worktree_path=row["worktree_path"],
        review_notes=row["review_notes"],
        diff=row["diff"],
        base_sha=row["base_sha"],
        merge_sha=row["merge_sha"],
    )
