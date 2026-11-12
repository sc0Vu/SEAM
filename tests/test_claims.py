"""The plan claim check (seam_merge/coordinator/claims.py), run with real pytest on a
throwaway snapshot. The probes here are the test's own code, so running them on the
host is fine; in SEAM they only ever run in the sandbox."""

import subprocess
import sys
from pathlib import Path

import pytest

from seam_merge.coordinator.claims import (
    CLAIM_WORDS,
    ClaimResult,
    UnprobedClaim,
    check_claims,
    unprobed_claims,
)
from seam_merge.coordinator.lead import PlanError, plan_from_dict
from seam_merge.sandbox.base import RunResult

# Like boltons' rotate_file before the fix: a missing file is silently ignored.
ROTATE = "import os\n\ndef rotate(path):\n    if not os.path.exists(path):\n        return\n"

# r3's original plan claimed the opposite. Its probe fails on the real code.
R3_FALSE = ("import pytest\nfrom rot import rotate\n\n"
            "def test_missing_raises(tmp_path):\n"
            "    with pytest.raises(FileNotFoundError):\n"
            "        rotate(tmp_path / 'nope')\n")
R3_TRUE = ("from rot import rotate\n\n"
           "def test_missing_is_ignored(tmp_path):\n"
           "    assert rotate(tmp_path / 'nope') is None\n")


class LocalPytest:
    """Runs the command with this interpreter's pytest in the snapshot."""

    def __init__(self):
        self.calls = []

    def run(self, cmd, workdir: Path, timeout):
        self.calls.append(cmd)
        argv = [sys.executable, *cmd.split()[1:]]
        p = subprocess.run(argv, cwd=workdir, capture_output=True, text=True, timeout=timeout)
        return RunResult(p.returncode, p.stdout + p.stderr)


@pytest.fixture
def snapshot(tmp_path):
    (tmp_path / "rot.py").write_text(ROTATE)
    return tmp_path


def task(key, *probes):
    return {"key": key, "claim_probes": [{"name": n, "code": c} for n, c in probes]}


def test_the_r3_claim_is_caught_and_a_true_one_holds(snapshot):
    got = check_claims([task("fix_rotate", ("missing_raises", R3_FALSE),
                             ("missing_ignored", R3_TRUE))], snapshot, LocalPytest())
    assert [(c.name, c.outcome) for c in got] == [("missing_raises", "false"),
                                                 ("missing_ignored", "holds")]
    assert "FileNotFoundError" in got[0].detail


def test_a_probe_that_cannot_import_is_a_false_claim(snapshot):
    bad_import = "from rot import does_not_exist\n\ndef test_x():\n    assert 1\n"
    (got,) = check_claims([task("t", ("api", bad_import))], snapshot, LocalPytest())
    assert got.outcome == "false"


def test_invalid_python_is_reported_and_never_run(snapshot):
    box = LocalPytest()
    (got,) = check_claims([task("t", ("broken", "def test_x(:\n"))], snapshot, box)
    assert got.outcome == "invalid" and box.calls == []


def test_results_follow_plan_order_across_tasks(snapshot):
    got = check_claims([task("a", ("one", R3_TRUE)), task("b"), task("c", ("two", R3_FALSE))],
                       snapshot, LocalPytest())
    assert [(c.task, c.outcome) for c in got] == [("a", "holds"), ("c", "false")]


def test_no_probes_means_no_sandbox_run(snapshot):
    box = LocalPytest()
    assert check_claims([task("a"), {"key": "b"}], snapshot, box) == [] and box.calls == []


def test_a_sandbox_that_cannot_run_pytest_is_not_a_verdict(snapshot):
    class NoPytest:
        def run(self, cmd, workdir, timeout):
            return RunResult(127, "pytest: not found")

    (got,) = check_claims([task("t", ("x", R3_TRUE))], snapshot, NoPytest())
    assert got == ClaimResult("t", "x", "not run", "pytest did not run (exit 127)")


@pytest.mark.parametrize("bad", [
    "not a list", [{"name": "x"}], [{"code": "c"}], [{"name": "", "code": "c"}],
    [{"name": "x", "code": "c"}, {"name": "x", "code": "d"}],
])
def test_plans_validate_claim_probes(bad):
    with pytest.raises(PlanError, match="claim_probes"):
        plan_from_dict({"goal": "g", "tasks": [{"key": "a", "type": "backend",
                                                "description": "d", "file_scope": ["x"],
                                                "claim_probes": bad}]})


# -- unprobed claims ------------------------------------------------------------------

# The sentence from the replay rerun's r1 plan, the one that escalated: it states
# current behavior, no probe covered it, and it was false under pytest (doc §12).
R1_SPEC = ("Treat a missing full_trace as empty. "
           "Behavior for wrapped exceptions that do have full_trace is unchanged.")


def test_a_task_stating_current_behavior_without_a_probe_is_flagged():
    (u,) = unprobed_claims([{"key": "fix", "spec_ref": R1_SPEC}])
    assert u == UnprobedClaim("fix", 1, "Behavior for wrapped exceptions that do have "
                                        "full_trace is unchanged.")


def test_probed_tasks_and_claim_free_specs_are_not_flagged():
    probed = {"key": "a", "spec_ref": R1_SPEC, "claim_probes": [{"name": "x", "code": "c"}]}
    plain = {"key": "b", "spec_ref": "Add lerp(a, b, t) returning a + (b - a) * t."}
    assert unprobed_claims([probed, plain, {"key": "c"}]) == []


@pytest.mark.parametrize("sentence", [
    "Existing callers work.", "Signature unchanged.", "None still means last.",
    "It keeps the order.", "Preserves identity.", "Currently it raises.",
])
def test_every_claim_word_counts(sentence):
    assert len(unprobed_claims([{"key": "k", "spec_ref": sentence}])) == 1


def test_known_false_alarm_a_parameter_named_keep():
    """A wording heuristic: r3's `keep` parameter reads as a claim word. Kept as a
    warning for the human, not a block, for exactly this reason."""
    assert unprobed_claims([{"key": "r3", "spec_ref": "keep >= 1 else ValueError."}])


def test_the_planner_is_told_the_same_words():
    from seam_merge.planners.claude_code import ClaudeCodePlanner

    command = ClaudeCodePlanner(None).command("goal")
    assert all(f'"{w}"' in command for w in CLAIM_WORDS)



def test_seam_plan_prints_unchecked_statements(capsys):
    from seam_merge import cli

    cli._print_claims([ClaimResult("a", "x", "holds")],
                      [UnprobedClaim("fix", 3, "Behavior is unchanged.")])
    out = capsys.readouterr().out
    assert "1/1 hold" in out
    assert 'UNCHECKED: fix states current behavior with no probe (+2 more): ' \
           '"Behavior is unchanged."' in out
