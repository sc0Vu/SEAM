"""Pluggable worker runtimes behind one interface (invariant 8)."""

from workers.base import Worker, WorkerFailed, WorkerResult

__all__ = ["Worker", "WorkerFailed", "WorkerResult"]
