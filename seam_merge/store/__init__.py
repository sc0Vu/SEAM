"""Task store: shared truth for task state (sqlite v1 -> postgres)."""

from seam_merge.store.sqlite import TaskStore

__all__ = ["TaskStore"]
