"""SEAM (Software Engineering Agent Merge): many agents, one codebase, one seam.

One lead plans file-scoped tasks, workers implement them in isolated git worktrees,
an independent reviewer and a serialized merge engine integrate them. See
doc/architecture.md for the design.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("seam-merge")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
