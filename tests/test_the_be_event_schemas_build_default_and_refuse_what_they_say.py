"""The messages igni sends a client: tags, defaults, builders from Agno objects, and what validation refuses."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.protocol.json_value import JSON_VALUE_SCHEMA
from igni_core.protocol.schemas import be_events
from igni_core.protocol.schemas.be_events import (
    CommandResult,
    ContentDelta,
    HITLRequest,
    RunPaused,
    SchedulerEvent,
    SessionListEntry,
    SessionListResult,
    StatusUpdate,
    TaskCreated,
    TaskSnapshot,
    TaskStateUpdated,
    ToolCompleted,
)
from igni_core.protocol.schemas.enums import CommandAction, CommandResultKind
from igni_core.protocol.schemas.envelope import Direction, Message

CLASSES = [getattr(be_events, name) for name in be_events.__all__]

TAGS = {
    "ContentDelta": "content_delta",
    "ToolStarted": "tool_started",
    "ToolCompleted": "tool_completed",
    "ToolError": "tool_error",
    "ModelCompleted": "model_completed",
    "RunStarted": "run_started",
    "RunCompleted": "run_completed",
    "StreamingDone": "streaming_done",
    "RunError": "run_error",
    "ReasoningStarted": "reasoning_started",
    "HITLRequest": "hitl_request",
    "TaskCreated": "task_created",
    "TaskUpdated": "task_updated",
    "TaskIteration": "task_iteration",
    "TaskStateUpdated": "task_state_updated",
    "CommandResult": "command_result",
    "StatusUpdate": "status_update",
    "SessionListResult": "session_list_result",
    "SessionCleared": "session_cleared",
    "Info": "info",
    "Error": "error",
    "SchedulerEvent": "scheduler_event",
    "RunPaused": "run_paused",
}


def _named_lookup(name: str, default: str = "") -> str:
    return f"friendly:{name}" if name else default


def test_every_message_has_its_tag_and_goes_to_the_client():
    messages = [c for c in CLASSES if issubclass(c, Message)]
    assert {c.__name__ for c in messages} == set(TAGS)
    for cls in messages:
        assert cls().type == TAGS[cls.__name__]
        assert cls.direction is Direction.TO_CLIENT


def test_every_exported_name_is_a_class_of_the_module_and_the_list_has_no_repeats():
    assert len(be_events.__all__) == len(set(be_events.__all__)) == 25
    for cls in CLASSES:
        assert cls.__module__ == be_events.__name__


def test_every_field_of_every_model_carries_a_published_description():
    for cls in CLASSES:
        for name, field in cls.model_fields.items():
            if name == "type":
                continue
            assert field.description, f"{cls.__name__}.{name}"
        assert cls.__doc__ and cls.__doc__.strip(), cls.__name__


def test_every_model_in_the_module_is_exported():
    public = {
        name
        for name, obj in vars(be_events).items()
        if isinstance(obj, type)
        and issubclass(obj, BaseModel)
        and obj.__module__ == be_events.__name__
    }
    assert public == set(be_events.__all__)


def test_the_defaults_of_the_status_and_command_messages():
    status = StatusUpdate()
    assert status.model_configured is True
    assert status.permission_mode == "default"
    assert status.cloud_connected is False
    assert status.theme is None
    assert status.input_tokens == status.output_tokens == status.max_context == 0
    result = CommandResult()
    assert result.kind is CommandResultKind.INFO
    assert result.action is CommandAction.NONE
    assert result.content == result.display_content == ""
    assert TaskCreated().status == "pending"
    assert ContentDelta().is_thinking is False
    flags = ToolCompleted()
    assert (flags.is_error, flags.is_rejected, flags.diff_rows) == (False, False, None)


def test_a_command_result_with_an_unlisted_kind_or_action_is_refused_at_construction():
    with pytest.raises(ValidationError):
        CommandResult(kind="loud")
    with pytest.raises(ValidationError):
        CommandResult(action="explode")
    assert CommandResult(kind="markdown", action="quit").action is CommandAction.QUIT
    assert CommandResult(action="").action is CommandAction.NONE


def test_a_status_text_unknown_to_the_enums_survives_verbatim():
    assert TaskCreated(status="blocked-on-review").status == "blocked-on-review"
    assert SchedulerEvent(event_type="paused").event_type == "paused"
    assert StatusUpdate(permission_mode="someFutureMode").permission_mode == "someFutureMode"
    assert TaskSnapshot(status="weird").status == "weird"


def test_a_session_listing_row_keeps_the_fields_it_does_not_know():
    row = SessionListEntry.model_validate({"session_id": "s1", "branch": "main", "pinned": True})
    assert row.model_extra == {"branch": "main", "pinned": True}
    dumped = row.model_dump()
    assert dumped["branch"] == "main" and dumped["pinned"] is True
    assert SessionListEntry.model_validate(dumped) == row
    listing = SessionListResult.model_validate({"sessions": [{"session_id": "s1", "x": 1}]})
    assert listing.sessions[0].model_dump()["x"] == 1


def test_tool_args_and_theme_are_published_as_objects_of_any_json_value():
    any_json = JSON_VALUE_SCHEMA
    schema = HITLRequest.model_json_schema()["properties"]["tool_args"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] == any_json
    theme = StatusUpdate.model_json_schema()["properties"]["theme"]
    assert {"type": "null"} in theme["anyOf"]
    assert {"type": "object", "additionalProperties": any_json} in [
        {k: v for k, v in branch.items() if k != "description"} for branch in theme["anyOf"]
    ]
    assert SessionListEntry.model_json_schema()["additionalProperties"] == any_json


def test_tool_args_hold_any_json_value():
    args = {"s": "x", "n": 1, "f": 1.5, "b": True, "z": None, "l": [1, {"a": []}], "d": {"k": "v"}}
    assert HITLRequest(tool_args=args).tool_args == args
    assert StatusUpdate(theme={"accent": "#fff", "n": 2}).theme == {"accent": "#fff", "n": 2}


def test_tool_args_must_still_be_an_object():
    with pytest.raises(ValidationError):
        HITLRequest(tool_args=["a"])
    with pytest.raises(ValidationError):
        StatusUpdate(theme="dark")


def test_diff_rows_come_back_as_pairs_and_a_wrong_arity_is_refused():
    done = ToolCompleted(diff_rows=[["+ a", "add"], ("- b", "del")])
    assert done.diff_rows == [("+ a", "add"), ("- b", "del")]
    with pytest.raises(ValidationError):
        ToolCompleted(diff_rows=[["only one"]])
    with pytest.raises(ValidationError):
        ToolCompleted(diff_rows=[["a", "b", "c"]])


def test_token_counts_take_whole_numbers_only():
    assert StatusUpdate(input_tokens="12").input_tokens == 12
    assert StatusUpdate(input_tokens=3.0).input_tokens == 3
    with pytest.raises(ValidationError):
        StatusUpdate(input_tokens=1.5)


def test_raw_dicts_become_models_inside_a_task_update_and_a_pause():
    update = TaskStateUpdated.model_validate({"tasks": [{"task_id": "t", "status": "running"}]})
    assert isinstance(update.tasks[0], TaskSnapshot)
    paused = RunPaused.model_validate({"requirements": [{"requirement_id": "r1"}]})
    assert isinstance(paused.requirements[0], HITLRequest)
    assert paused.requirements[0].requirement_id == "r1"


def test_text_passes_through_unchanged():
    text = "héllo ☃ \U0001f600 " + "x" * 200_000
    assert ContentDelta(text=text).text == text


def _requirement(name="shell", args=None, with_exec=True):
    execution = SimpleNamespace(tool_name=name, tool_args=args)
    return SimpleNamespace(tool_execution=execution) if with_exec else SimpleNamespace()


def test_a_request_is_built_from_an_agno_requirement():
    requirement = _requirement("shell", {"cmd": "ls"})
    request = HITLRequest.from_agno_requirement(requirement, _named_lookup)
    assert request.requirement_id == str(id(requirement))
    assert request.tool_name == "shell"
    assert request.friendly_name == "friendly:shell"
    assert request.tool_args == {"cmd": "ls"}
    assert request.details == "" and request.details_pending is False
    assert request.type == "hitl_request"


def test_the_friendly_name_lookup_is_called_with_the_name_and_an_empty_default():
    calls = []

    def lookup(name, default="x"):
        calls.append((name, default))
        return "n"

    HITLRequest.from_agno_requirement(_requirement("t", {}), lookup)
    assert calls == [("t", "")]


def test_the_requests_args_are_a_copy():
    args = {"k": "v"}
    request = HITLRequest.from_agno_requirement(_requirement("t", args), _named_lookup)
    args["k"] = "changed"
    assert request.tool_args == {"k": "v"}


@pytest.mark.parametrize(
    "requirement",
    [SimpleNamespace(), SimpleNamespace(tool_execution=None)],
    ids=["no-attribute", "none"],
)
def test_a_requirement_without_an_execution_gives_an_empty_name_and_args(requirement):
    request = HITLRequest.from_agno_requirement(requirement, _named_lookup)
    assert request.tool_name == ""
    assert request.friendly_name == ""
    assert request.tool_args == {}


def test_a_failing_lookup_is_not_hidden():
    def boom(name, default=""):
        raise KeyError(name)

    with pytest.raises(KeyError):
        HITLRequest.from_agno_requirement(_requirement("t", {}), boom)


def test_a_snapshot_is_read_from_an_agno_task():
    task = SimpleNamespace(task_id="t1", title="Write", status="running", assignee="ann")
    snap = TaskSnapshot.from_agno(task)
    assert snap.model_dump() == {
        "task_id": "t1",
        "title": "Write",
        "status": "running",
        "assignee": "ann",
    }


def test_a_snapshot_of_an_unassigned_task_or_a_bare_object_is_empty():
    assert TaskSnapshot.from_agno(SimpleNamespace(task_id="t", assignee=None)).assignee == ""
    bare = TaskSnapshot.from_agno(SimpleNamespace())
    assert bare.model_dump() == {"task_id": "", "title": "", "status": "", "assignee": ""}


def test_a_snapshot_status_that_is_not_a_string_is_written_as_its_text():
    from igni_core.protocol.schemas.enums import OrchestrationTaskStatus

    snap = TaskSnapshot.from_agno(SimpleNamespace(status=OrchestrationTaskStatus.FAILED))
    assert snap.status == "failed"
    assert TaskSnapshot.from_agno(SimpleNamespace(status=3)).status == "3"


def test_a_requirement_whose_execution_has_no_args_or_name_gives_empty_ones_not_a_crash():
    request = HITLRequest.from_agno_requirement(_requirement("shell", None), _named_lookup)
    assert request.tool_args == {}
    nameless = HITLRequest.from_agno_requirement(_requirement(None, {"a": 1}), _named_lookup)
    assert nameless.tool_name == ""
    assert nameless.tool_args == {"a": 1}


def test_a_snapshot_of_an_unassigned_task_or_a_bare_object_is_empty_not_the_word_none():
    assert TaskSnapshot.from_agno(SimpleNamespace(task_id="t", assignee=None)).assignee == ""
    bare = TaskSnapshot.from_agno(SimpleNamespace())
    assert bare.model_dump() == {"task_id": "", "title": "", "status": "", "assignee": ""}
    nulls = TaskSnapshot.from_agno(
        SimpleNamespace(task_id=None, title=None, status=None, assignee=None)
    )
    assert nulls.model_dump() == {"task_id": "", "title": "", "status": "", "assignee": ""}
