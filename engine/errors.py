"""Typed exceptions raised by the engine and store. Only the CLI prints or exits."""


class SeamError(Exception):
    """Base class for all SEAM errors."""


class IllegalTransition(SeamError):
    """An event was applied to a task in a state that does not accept it."""

    def __init__(self, state: str, event: str) -> None:
        super().__init__(f"event {event!r} is not legal from state {state!r}")
        self.state = state
        self.event = event


class TaskNotFound(SeamError):
    """No task exists with the requested id."""


class StaleState(SeamError):
    """The task's state changed underneath a compare-and-swap update."""


class MergeSlotBusy(SeamError):
    """Another task already occupies the single global `merging` slot (invariant 4)."""


class GitError(SeamError):
    """A git command failed unexpectedly."""


class TargetCheckedOut(SeamError):
    """The merge target branch is checked out in a working tree.

    Moving it would leave that tree's files stale, so the engine refuses: run SEAM
    against a dedicated clone (or detach the target there).
    """
