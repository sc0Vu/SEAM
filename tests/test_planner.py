import json
import shlex

import pytest

from coordinator.lead import PlanError, load_plan, plan_from_dict
from coordinator.planner import Planner, PlannerFailed
from planners.claude_code import PLAN_SCHEMA, ClaudeCodePlanner, plan_to_json
from sandbox.base import RunResult

DRAFT = {
    "goal": "url-query",
    "notes": "urlutils tasks overlap and will serialize",
    "tasks": [
        {"key": "use_it", "type": "backend", "description": "use setlist",
         "spec_ref": "c", "file_scope": ["u.py"], "blocked_by": ["setlist"], "test_cmd": "t"},
        {"key": "setlist", "type": "backend", "description": "add setlist",
         "spec_ref": "c", "file_scope": ["d.py"], "blocked_by": [], "test_cmd": "t"},
    ],
}


class FakeSandbox:
    def __init__(self, output, exit_code=0):
        self.output, self.exit_code, self.calls = output, exit_code, []

    def run(self, cmd, workdir, timeout):
        self.calls.append((cmd, workdir))
        return RunResult(self.exit_code, "log noise\n" + self.output + "\n")


def result(**kw):
    return json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.4,
                       "usage": {"input_tokens": 10, "output_tokens": 900}, **kw})


def test_is_a_planner():
    assert isinstance(ClaudeCodePlanner(FakeSandbox("")), Planner)


def test_draft_is_validated_and_ordered_like_a_hand_written_plan(tmp_path):
    sb = FakeSandbox(result(structured_output=DRAFT))
    draft = ClaudeCodePlanner(sb).plan("edit URL queries", tmp_path)
    assert [t["key"] for t in draft.plan.tasks] == ["setlist", "use_it"]  # deps first
    assert draft.notes.startswith("urlutils") and draft.cost_usd == 0.4
    assert draft.usage.output_tokens == 900
    assert sb.calls[0][1] == tmp_path  # runs over the snapshot it was given


def test_runs_read_only_with_the_plan_schema_and_the_goal():
    argv = shlex.split(ClaudeCodePlanner(FakeSandbox(""), model="m").command("edit URL queries"))
    assert argv[:2] == ["claude", "-p"]
    assert set(argv[argv.index("--tools") + 1].split(",")) == {"Read", "Glob", "Grep"}
    assert json.loads(argv[argv.index("--json-schema") + 1]) == PLAN_SCHEMA
    assert "edit URL queries" in argv[2] and "blocked_by" in argv[2]
    assert argv[argv.index("--model") + 1] == "m"


@pytest.mark.parametrize("output, msg", [
    ("not json", "no Claude Code result"),
    (result(is_error=True, result="rate limited"), "no plan"),
    (result(structured_output={"goal": "g", "notes": "", "tasks": [
        {"key": "a", "type": "backend", "description": "d", "spec_ref": "",
         "file_scope": ["a"], "blocked_by": ["ghost"], "test_cmd": ""}]}), "invalid plan"),
])
def test_failures_are_planner_failed_with_cost(tmp_path, output, msg):
    with pytest.raises(PlannerFailed, match=msg) as e:
        ClaudeCodePlanner(FakeSandbox(output)).plan("g", tmp_path)
    assert e.value.cost_usd == (0.0 if output == "not json" else 0.4)


def test_written_plan_round_trips_through_load_plan(tmp_path):
    draft = ClaudeCodePlanner(FakeSandbox(result(structured_output=DRAFT))).plan("g", tmp_path)
    path = tmp_path / "plan.json"
    path.write_text(plan_to_json(draft, "edit URL queries"))
    data = json.loads(path.read_text())
    assert data["goal_text"] == "edit URL queries" and "serialize" in data["planner_notes"]
    assert load_plan(path).tasks == draft.plan.tasks


@pytest.mark.parametrize("bad", [None, [], {"goal": "g", "tasks": ["not a task"]}])
def test_plan_from_dict_rejects_malformed_input(bad):
    with pytest.raises(PlanError):
        plan_from_dict(bad)
