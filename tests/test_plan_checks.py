"""Plan checks: test commands, and what a revision changed (plan_checks.py)."""

import pytest

from seam_merge.coordinator.plan_checks import (
    SuiteGap,
    TaskChange,
    check_test_cmds,
    plan_changes,
)

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


BASE = [
    {"key": "logic", "type": "frontend", "description": "grid logic, edited by a human",
     "file_scope": ["src/logic/**"], "blocked_by": [], "test_cmd": "node --test"},
    {"key": "ui", "type": "frontend", "description": "render the grid",
     "file_scope": ["src/ui/**"], "blocked_by": ["logic"], "test_cmd": "node --test"},
    {"key": "old", "type": "docs", "description": "obsolete", "file_scope": ["x.md"]},
]


def test_a_revision_reports_every_departure_from_the_base_by_key():
    revised = [
        {**BASE[1], "blocked_by": ["logic", "docs"]},  # reordered and changed
        BASE[0],                                         # kept as is
        {"key": "docs", "type": "docs", "description": "README", "file_scope": ["*.md"]},
    ]
    assert plan_changes(BASE, revised) == [
        TaskChange("old", "removed"),
        TaskChange("ui", "changed", ("blocked_by",)),
        TaskChange("docs", "added"),
    ]


def test_a_field_the_planner_only_filled_with_an_empty_value_is_not_a_change():
    revised = [{**t, "may_change_tests": [], "claim_probes": [], "blocked_by":
                t.get("blocked_by", [])} for t in BASE]
    assert plan_changes(BASE, revised) == []


def test_an_edited_description_is_reported_as_changed():
    revised = [{**BASE[0], "description": "rewritten"}, *BASE[1:]]
    assert plan_changes(BASE, revised) == [TaskChange("logic", "changed", ("description",))]
