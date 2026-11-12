"""The boltons replay benchmark's pure logic: which text becomes the goal, and how the
hidden tests are scored. (Running it needs Docker and a boltons clone; see
examples/boltons-replay/README.md.)"""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).parent.parent / "examples" / "boltons-replay" / "replay.py"
_spec = importlib.util.spec_from_file_location("replay", _PATH)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


def test_change_section_keeps_only_the_change_when_named():
    body = ("## The change\n\nFix X.\n\n## The backstory\n\nFound it.\n\n"
            "## AI assistance\n\n- Model: m\n")
    assert R.change_section(body) == "Fix X."


def test_change_section_drops_how_it_was_made_when_unnamed():
    body = "Fix Y.\r\n\r\n## Validation\r\nok\r\n\r\n## AI assistance\r\n- m\r\n"
    assert R.change_section(body) == "Fix Y.\nok"
    assert R.change_section("plain text") == "plain text"
    assert R.change_section("") == ""


def test_every_goal_has_its_pr_texts_and_the_same_wrapper():
    for g in R.MANIFEST["goals"]:
        text = R.goal_text(g)
        assert text.startswith("Fix the following problems in boltons")
        assert text.endswith(f"({R.MANIFEST['suite_cmd']}) must stay green.")
        assert all(f"(boltons#{n})" in text for n in g["prs"])
        assert "## AI assistance" not in text and "## The backstory" not in text


JUNIT = """<testsuites><testsuite>
<testcase classname="tests.t" name="ok"/>
<testcase classname="tests.t" name="bad"><failure message="x"/></testcase>
<testcase classname="tests.t" name="err"><error message="x"/></testcase>
<testcase classname="tests.t" name="skip"><skipped message="x"/></testcase>
</testsuite></testsuites>"""


def test_outcomes_from_junit():
    assert R.outcomes(JUNIT) == {"tests.t::ok": "passed", "tests.t::bad": "failed",
                                 "tests.t::err": "failed", "tests.t::skip": "skipped"}


@pytest.mark.parametrize("seam, passed, failed, regressions", [
    ({"new1": "passed", "new2": "passed", "old": "passed"}, 2, [], []),
    ({"new1": "passed", "new2": "failed", "old": "passed"}, 1, ["new2"], []),
    ({"new1": "passed", "old": "failed"}, 1, ["new2"], ["old"]),  # new2 never ran
])
def test_compare_scores_target_tests_and_regressions(seam, passed, failed, regressions):
    parent = {"new1": "failed", "old": "passed", "flaky": "passed"}  # new2 absent
    fix = {"new1": "passed", "new2": "passed", "old": "passed", "flaky": "failed"}
    got = R.compare(parent, fix, seam)
    assert got["target"] == 2  # fail (or absent) on the parent, pass on the fix
    assert (got["target_passed"], got["target_failed"], got["regressions"]) == (
        passed, failed, regressions)
