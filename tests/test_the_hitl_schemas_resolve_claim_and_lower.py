"""The HITL schemas: what a requirement asks about, one run per batch, the
strict action enum and the tool arguments handed to the permission check."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_hitl import (
    ApplyDecisionResult,
    HitlAction,
    PersistChoiceResult,
    RequirementTool,
    ResolvedBatch,
    ToolCallArgs,
)
from igni_core.protocol import messages as msg


def _req(execution):
    return SimpleNamespace(tool_execution=execution)


def test_a_requirement_names_its_tool_and_arguments():
    execution = SimpleNamespace(tool_name="shell", tool_args={"command": "ls"})
    tool = RequirementTool.of(_req(execution))
    assert (tool.name, tool.args) == ("shell", {"command": "ls"})


def test_a_requirement_without_an_execution_asks_about_no_tool():
    tool = RequirementTool.of(_req(None))
    assert (tool.name, tool.args) == ("", {})


def test_an_execution_with_none_fields_asks_about_no_tool():
    tool = RequirementTool.of(_req(SimpleNamespace(tool_name=None, tool_args=None)))
    assert (tool.name, tool.args) == ("", {})


def test_a_requirement_tool_cannot_be_changed():
    with pytest.raises(ValidationError):
        RequirementTool().name = "x"


def test_the_first_claim_fixes_the_batch_s_run_and_later_ones_must_match():
    batch = ResolvedBatch()
    assert batch.claim("run-1") is True
    assert batch.run_id == "run-1"
    assert batch.claim("run-1") is True
    assert batch.claim("run-2") is False
    assert batch.run_id == "run-1"


def test_a_batch_fixed_to_none_refuses_nothing_until_a_run_is_named():
    batch = ResolvedBatch()
    assert batch.claim(None) is True
    assert batch.claim(None) is True
    # None leaves the batch unfixed, so the next real id fixes it.
    assert batch.claim("run-1") is True
    assert batch.claim("run-2") is False


def test_batches_do_not_share_their_lists():
    a, b = ResolvedBatch(), ResolvedBatch()
    a.requirements.append(1)
    a.errors.append(msg.Error(message="x"))
    assert b.requirements == [] and b.errors == []


def test_the_action_is_strictly_confirm_or_reject():
    assert HitlAction("confirm") is HitlAction.CONFIRM
    assert HitlAction("reject") is HitlAction.REJECT
    for bad in ("CONFIRM", "approve", "", "unknown"):
        with pytest.raises(ValueError):
            HitlAction(bad)


def test_tool_arguments_keep_any_key_and_name_the_common_ones():
    args = ToolCallArgs(command="ls", custom={"nested": [1]}, flag=False)
    assert args.command == "ls"
    assert args.model_dump()["custom"] == {"nested": [1]}


def test_lowering_drops_empty_strings_and_none_but_keeps_zero_and_false():
    args = ToolCallArgs(file_path="a.txt", zero=0, off=False, blank="", gone=None, empty=[])
    assert args.as_dict() == {"file_path": "a.txt", "zero": 0, "off": False, "empty": []}


def test_lowering_nothing_gives_an_empty_dict():
    assert ToolCallArgs().as_dict() == {}


def test_the_arguments_publish_as_a_json_object_with_described_fields():
    schema = ToolCallArgs.model_json_schema()
    assert schema["additionalProperties"]["description"] == "Any JSON value."
    assert all(p.get("description") for p in schema["properties"].values())


def test_the_results_report_ok_rule_level_and_reason():
    ok = PersistChoiceResult(ok=True, rule="Bash(ls)", level="allow")
    assert (ok.ok, ok.rule, ok.level) == (True, "Bash(ls)", "allow")
    failed = PersistChoiceResult(ok=False, reason="bad choice")
    assert (failed.rule, failed.level, failed.reason) == ("", None, "bad choice")
    assert ApplyDecisionResult(ok=False, reason="boom").reason == "boom"
    assert ApplyDecisionResult(ok=True).reason == ""
