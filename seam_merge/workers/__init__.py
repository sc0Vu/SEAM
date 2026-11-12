"""Pluggable worker runtimes behind one interface (invariant 8)."""

from seam_merge.workers.base import Worker, WorkerFailed, WorkerResult

__all__ = ["Worker", "WorkerFailed", "WorkerResult"]
