"""Plan checks on test commands (seam_merge/coordinator/plan_checks.py)."""

import pytest

from seam_merge.coordinator.plan_checks import SuiteGap, check_test_cmds

SUITE = "python -m pytest -q --doctest-modules boltons tests"


def task(key, cmd):
    return {"key": key, "test_cmd": cmd}


def test_bench3s_gap_is_caught():
    """bench-3's planned test_cmd, verbatim: no doctests, while the merge suite ran them."""
    (gap,) = check_test_cmds([task("omd_setlist", "python -m pytest tests/test_dictutils.py -q")],
                             SUITE)
    assert gap.task == "omd_setlist" and "--doctest-modules" in gap.message


@pytest.mark.parametrize("cmd", [
    "python -m pytest -q --doctest-modules boltons/dictutils.py tests/test_dictutils.py",
    "pytest --doctest-modules=x tests",  # value form still counts
    "python -c \"import tomllib; tomllib.load(open('pyproject.toml','rb'))\"",  # not pytest
])
def test_matching_or_incomparable_commands_pass(cmd):
    assert check_test_cmds([task("t", cmd)], SUITE) == []


def test_a_task_without_tests_is_flagged():
    assert check_test_cmds([task("t", "  "), {"key": "u"}], SUITE) == [
        SuiteGap("t", "no test_cmd: review will run none of its tests"),
        SuiteGap("u", "no test_cmd: review will run none of its tests")]


def test_a_non_pytest_suite_is_not_compared():
    assert check_test_cmds([task("t", "pytest tests")], "go test ./...") == []


def test_unparseable_commands_are_not_compared():
    assert check_test_cmds([task("t", "pytest 'unclosed")], SUITE) == []


def test_the_planner_is_told_the_suite_only_when_known():
    from seam_merge.planners.claude_code import ClaudeCodePlanner

    told = ClaudeCodePlanner(None, suite_cmd=SUITE).command("g")
    assert "the FULL suite runs as: " + SUITE in told
    assert "FULL suite runs as" not in ClaudeCodePlanner(None).command("g")


def test_seam_plan_prints_the_gaps(capsys):
    from seam_merge import cli

    cli._print_gaps([SuiteGap("t", "test_cmd lacks --doctest-modules")])
    assert "WARNING test_cmd: t: test_cmd lacks --doctest-modules" in capsys.readouterr().out
