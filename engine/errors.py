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
