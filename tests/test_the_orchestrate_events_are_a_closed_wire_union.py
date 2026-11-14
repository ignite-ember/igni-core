"""The orchestrate events: one closed union, one reader, one wire shape."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from igni_core.core.tools.orchestrate_events import (
    ORCHESTRATE_EVENT_ADAPTER,
    AgentCompletedEvent,
    AgentPausedEvent,
    AgentStartedEvent,
    ContentPreviewEvent,
    RunErrorEvent,
    TaskCreatedEvent,
    TaskUpdatedEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    VisualizationDeltaEvent,
)

_EVENTS = [
    ToolStartedEvent(agent_path="a", tool="t", tool_call_id="c1", args="x=1"),
    ToolCompletedEvent(agent_path="a", tool="t", result="r", is_error=True),
    AgentStartedEvent(agent_path="a", agent="a", parent="p", run_id="r", task="do"),
    AgentPausedEvent(agent_path="a", count=3),
    AgentCompletedEvent(agent_path="a", input_tokens=1, output_tokens=2, reasoning_tokens=3),
    RunErrorEvent(agent_path="a", error="boom"),
    ContentPreviewEvent(agent_path="a", text="hi"),
    TaskCreatedEvent(agent_path="a", title="T", assignee="bob"),
    TaskUpdatedEvent(agent_path="a", status="done"),
    VisualizationDeltaEvent(agent_path="a", spec_id="s", spec_json="{}", final=True),
]


class TestTheUnion:
    @pytest.mark.parametrize("event", _EVENTS, ids=lambda e: e.type)
    def test_every_kind_round_trips_through_the_adapter(self, event):
        wire = event.model_dump(mode="json")
        parsed = ORCHESTRATE_EVENT_ADAPTER.validate_python(wire)
        assert type(parsed) is type(event)
        assert parsed == event

    def test_the_ten_kinds_have_ten_distinct_types(self):
        assert len({e.type for e in _EVENTS}) == 10

    def test_an_unknown_type_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            ORCHESTRATE_EVENT_ADAPTER.validate_python({"type": "nope", "agent_path": "a"})
        # The type names the kind: one error about the tag, not one per kind.
        assert [e["type"] for e in caught.value.errors()] == ["union_tag_invalid"]

    def test_a_missing_required_field_is_refused(self):
        with pytest.raises(ValidationError):
            ORCHESTRATE_EVENT_ADAPTER.validate_python({"type": "tool_started", "agent_path": "a"})

    def test_the_type_is_frozen(self):
        event = RunErrorEvent(agent_path="a", error="x")
        with pytest.raises(ValidationError):
            event.type = "tool_started"  # type: ignore[assignment]

    def test_defaults(self):
        assert AgentPausedEvent(agent_path="a").count == 1
        done = AgentCompletedEvent(agent_path="a")
        assert (done.is_error, done.input_tokens, done.output_tokens, done.reasoning_tokens) == (
            False,
            0,
            0,
            0,
        )
        tool = ToolCompletedEvent(agent_path="a", tool="t")
        assert (tool.result, tool.is_error, tool.tool_call_id, tool.card_id) == (
            "",
            False,
            None,
            None,
        )
        assert ToolStartedEvent(agent_path="a", tool="t").args == ""
        assert TaskCreatedEvent(agent_path="a").title == ""
        assert TaskUpdatedEvent(agent_path="a").status == ""


class TestTheCardId:
    def test_other_events_keep_their_card_id_on_the_wire(self):
        wire = RunErrorEvent(agent_path="a", error="x", card_id="card-1").model_dump(mode="json")
        assert wire["card_id"] == "card-1"

    def test_a_visualization_delta_leaves_it_off_the_wire(self):
        event = VisualizationDeltaEvent(agent_path="a", spec_id="s", spec_json="{}", card_id="c")
        assert event.card_id == "c"
        assert "card_id" not in event.model_dump(mode="json")


class TestTheVisualizationWireShape:
    def test_the_spec_travels_under_the_key_json(self):
        wire = VisualizationDeltaEvent(agent_path="a", spec_id="s", spec_json='{"k":1}').model_dump(
            mode="json", by_alias=True
        )
        assert wire["json"] == '{"k":1}'
        assert "spec_json" not in wire

    def test_either_name_is_accepted_on_input(self):
        by_alias = ORCHESTRATE_EVENT_ADAPTER.validate_python(
            {"type": "visualization_delta", "agent_path": "a", "spec_id": "s", "json": "{}"}
        )
        by_name = VisualizationDeltaEvent(agent_path="a", spec_id="s", spec_json="{}")
        assert by_alias == by_name


class TestFromPartialArgs:
    def _parse(self, args: str):
        return VisualizationDeltaEvent.from_partial_args(
            agent_path="v", spec_id="sid", args_partial=args
        )

    def test_empty_input_gives_nothing(self):
        assert self._parse("") is None

    def test_unreadable_input_gives_nothing(self):
        assert self._parse("not json at all") is None

    def test_an_error_that_is_not_unreadable_input_is_not_hidden(self, monkeypatch):
        def broken(*_a, **_k):
            raise RuntimeError("caller bug")

        monkeypatch.setattr("igni_core.core.tools.orchestrate_events.jiter.from_json", broken)
        with pytest.raises(RuntimeError, match="caller bug"):
            self._parse('{"spec": {}}')

    def test_a_root_that_is_not_an_object_gives_nothing(self):
        assert self._parse("[1, 2]") is None

    @pytest.mark.parametrize("args", ['{"spec": [1]}', '{"spec": "s"}', '{"other": {}}'])
    def test_a_spec_that_is_not_an_object_gives_nothing(self, args):
        assert self._parse(args) is None

    def test_a_truncated_document_is_salvaged(self):
        event = self._parse('{"spec": {"root": "r", "elem')
        assert event is not None
        assert json.loads(event.spec_json)["root"] == "r"
        assert (event.agent_path, event.spec_id, event.final) == ("v", "sid", False)

    def test_the_spec_json_escapes_non_ascii(self):
        event = self._parse('{"spec": {"t": "é"}}')
        assert event is not None
        assert event.spec_json == '{"t": "\\u00e9"}'

    def test_the_final_flag_is_passed_through(self):
        event = VisualizationDeltaEvent.from_partial_args(
            agent_path="v", spec_id="s", args_partial='{"spec": {}}', final=True
        )
        assert event is not None and event.final is True
