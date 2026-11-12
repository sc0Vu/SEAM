"""The single-writer lock on a state dir.

SQLite is valid in v1 only while ONE process writes task state (CLAUDE.md, Tech).
`run` is long-lived, and the human escalation commands and `add-goal` write too, so
every writer takes this lock first and a second writer is refused instead of racing
(e.g. a `cancel` landing on a task a running worker is about to submit). Readers
(`status`, `inspect`) need no lock: SQLite serves reads alongside one writer.

The lock is an OS file lock (`flock`), so it is released when its holder exits,
even on a crash or `kill -9`: no stale lock to clean up.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from seam_merge.engine.errors import SeamError

LOCK_NAME = "writer.lock"


class StoreBusy(SeamError):
    """Another process (usually a `run`) is writing this state dir."""


@contextmanager
def writer_lock(state_dir: Path, what: str) -> Iterator[None]:
    """Hold the state dir's writer lock for the body, or raise `StoreBusy` at once.

    `what` names the holder (e.g. "run"); it is written into the lock file so the
    refusal can say who holds it.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 200, 0).decode(errors="replace").strip() or "unknown"
            raise StoreBusy(f"{state_dir} is being written by another process ({holder}); "
                            "wait for it to finish or stop it first") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{what}, pid {os.getpid()}".encode(), 0)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def active_writer(state_dir: Path) -> str | None:
    """Who holds the writer lock right now, or None if nobody does (a read-only
    check: it takes the lock for an instant and releases it)."""
    path = Path(state_dir) / LOCK_NAME
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return os.pread(fd, 200, 0).decode(errors="replace").strip() or "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)
