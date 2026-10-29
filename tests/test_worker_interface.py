from pathlib import Path

import pytest

from engine.task import Task
from workers import Worker, WorkerResult


class EchoWorker:
    type = "backend"

    def run(self, task: Task, worktree: Path) -> WorkerResult:
        return WorkerResult(diff="--- a\n+++ b\n")


def test_adapter_satisfies_protocol_structurally():
    assert isinstance(EchoWorker(), Worker)


@pytest.mark.parametrize("kw", [{}, {"diff": "x", "scope_request": ("a/**",)}])
def test_result_must_be_exactly_one_outcome(kw):
    with pytest.raises(ValueError):
        WorkerResult(**kw)


def test_scope_request_is_a_valid_outcome():
    assert WorkerResult(scope_request=("lib/**",)).diff == ""
