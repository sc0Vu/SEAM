"""Pluggable worker runtimes behind one interface (invariant 8)."""

from workers.base import Worker, WorkerResult

__all__ = ["Worker", "WorkerResult"]
