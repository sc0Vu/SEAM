import json
import shlex

import pytest

from seam_merge.coordinator.lead import PlanError, load_plan, plan_from_dict
from seam_merge.coordinator.planner import Planner, PlannerFailed
from seam_merge.planners.claude_code import (
    MAX_BASE_CHARS,
    PLAN_SCHEMA,
    ClaudeCodePlanner,
    plan_to_json,
)
from seam_merge.sandbox.base import RunResult

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


def test_planner_declares_which_existing_tests_a_task_may_rewrite(tmp_path):
    item = PLAN_SCHEMA["properties"]["tasks"]["items"]
    assert "may_change_tests" in item["required"]
    draft = json.loads(json.dumps(DRAFT))
    draft["tasks"][1]["may_change_tests"] = ["tests/test_d.py::test_old"]
    sb = FakeSandbox(result(structured_output=draft))
    plan = ClaudeCodePlanner(sb).plan("goal", tmp_path).plan
    assert {t["key"]: t.get("may_change_tests") for t in plan.tasks}["setlist"] == [
        "tests/test_d.py::test_old"]
    assert "may_change_tests:" in ClaudeCodePlanner(sb).command("goal")


def test_planner_backs_current_behavior_claims_with_probes(tmp_path):
    from seam_merge.coordinator.claims import ClaimResult, UnprobedClaim

    item = PLAN_SCHEMA["properties"]["tasks"]["items"]
    assert "claim_probes" in item["required"]
    assert "claim_probes:" in ClaudeCodePlanner(FakeSandbox("")).command("goal")
    draft = json.loads(json.dumps(DRAFT))
    draft["tasks"][1]["claim_probes"] = [{"name": "old", "code": "def test_x(): pass\n"}]
    plan = ClaudeCodePlanner(FakeSandbox(result(structured_output=draft))).plan("g", tmp_path)
    written = json.loads(plan_to_json(
        plan, "goal text", claim_check=[ClaimResult("setlist", "old", "holds")],
        unprobed_claims=[UnprobedClaim("use_it", 1, "x is unchanged.")]))
    assert written["unprobed_claims"] == [{"task": "use_it", "sentences": 1,
                                           "excerpt": "x is unchanged."}]
    assert written["claim_check"] == [{"task": "setlist", "name": "old", "outcome": "holds",
                                       "detail": ""}]
    probes = {t["key"]: t.get("claim_probes") for t in written["tasks"]}
    assert probes["setlist"][0]["name"] == "old"


def test_without_a_base_the_prompt_has_no_base_plan():
    assert "BASE PLAN" not in ClaudeCodePlanner(FakeSandbox("")).command("goal")


def test_a_base_plan_is_given_to_the_planner_to_revise(tmp_path):
    base = plan_from_dict(DRAFT)
    revised = json.loads(json.dumps(DRAFT))
    revised["tasks"].append({"key": "docs", "type": "docs", "description": "README",
                             "spec_ref": "", "file_scope": ["README.md"],
                             "blocked_by": ["use_it"], "test_cmd": "t"})
    sb = FakeSandbox(result(structured_output=revised))
    draft = ClaudeCodePlanner(sb).plan("add a README", tmp_path, base=base)
    prompt = shlex.split(sb.calls[0][0])[2]
    assert "BASE PLAN" in prompt and "keep each of its tasks exactly" in prompt
    assert '"add setlist"' in prompt and "add a README" in prompt  # base tasks + goal
    assert [t["key"] for t in draft.plan.tasks][-1] == "docs"


def test_an_oversized_base_plan_fails_before_any_model_call(tmp_path):
    big = {"goal": "g", "tasks": [{"key": "a", "type": "backend",
                                   "description": "x" * MAX_BASE_CHARS, "file_scope": ["a"]}]}
    sb = FakeSandbox(result(structured_output=DRAFT))
    with pytest.raises(PlannerFailed, match="base plan is"):
        ClaudeCodePlanner(sb).plan("g", tmp_path, base=plan_from_dict(big))
    assert sb.calls == []


def test_merged_base_tasks_are_named_so_the_planner_does_not_replan_them():
    base = plan_from_dict({"goal": "g", "tasks": [DRAFT["tasks"][0] | {"blocked_by": []}]})
    planner = ClaudeCodePlanner(FakeSandbox(""))
    prompt = shlex.split(planner.command("g", base, [DRAFT["tasks"][1]]))[2]
    assert "ALREADY MERGED" in prompt and '"setlist"' in prompt
    assert "ALREADY MERGED" not in shlex.split(planner.command("g", base))[2]
