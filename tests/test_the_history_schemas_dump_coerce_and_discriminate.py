"""The chat-history schemas: turn wire shapes, Agno views, search hit.

A rebuilt chat reaches the client as ``model_dump`` of these turns, so the
role literal, the key set (``created_at`` on conversational turns only) and
the discriminated union are the contract. The Agno views absorb None from a
persisted run so the walker can treat every field as populated.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from igni_core.backend import schemas_history as sh
from igni_core.backend.schemas_history import (
    AgnoRunMessageView,
    AgnoRunMetricsView,
    AgnoRunView,
    AssistantTurn,
    ChatHistoryEntry,
    ChatSearchHit,
    ChatTurn,
    PlanTurn,
    StatsTurn,
    ThinkingTurn,
    ToolTurn,
    UserTurn,
    VisualizationTurn,
)
from igni_core.protocol import messages as msg

_CHAT_TURN = TypeAdapter(ChatTurn)

_CONVERSATIONAL = [
    (UserTurn, "user"),
    (AssistantTurn, "assistant"),
    (ThinkingTurn, "thinking"),
    (ToolTurn, "tool"),
    (PlanTurn, "plan"),
]


@pytest.mark.parametrize(("cls", "role"), _CONVERSATIONAL)
def test_a_conversational_turn_dumps_its_role_run_and_time(cls, role):
    dumped = cls(run_id="r1", created_at=12).model_dump()
    assert dumped["role"] == role
    assert dumped["run_id"] == "r1"
    assert dumped["created_at"] == 12


@pytest.mark.parametrize("cls", [StatsTurn, VisualizationTurn])
def test_stats_and_visualization_turns_carry_a_run_but_no_time(cls):
    dumped = cls(run_id="r1").model_dump()
    assert dumped["run_id"] == "r1"
    assert "created_at" not in dumped


def test_a_turn_refuses_another_turns_role():
    with pytest.raises(ValidationError):
        UserTurn(role="assistant")


def test_a_tool_turn_defaults_to_a_call_that_neither_failed_nor_was_rejected():
    assert ToolTurn().model_dump() == {
        "role": "tool",
        "run_id": "",
        "created_at": 0,
        "tool_name": "",
        "friendly_name": "",
        "args": "",
        "content": "",
        "is_error": False,
        "is_rejected": False,
    }


def test_a_visualization_keeps_the_name_an_extension_gave_it():
    turn = VisualizationTurn(
        run_id="r",
        spec_id="s",
        spec={"a": [1, {"b": None}]},
        title="Burn-down",
        extension_id="ext.charts",
        seq=7,
    )
    dumped = turn.model_dump()
    assert (dumped["title"], dumped["extension_id"], dumped["seq"]) == (
        "Burn-down",
        "ext.charts",
        7,
    )
    assert dumped["spec"] == {"a": [1, {"b": None}]}


def test_a_built_in_visualization_has_the_visualizer_and_no_extension():
    dumped = VisualizationTurn().model_dump()
    assert dumped["source_agent"] == "visualizer"
    assert dumped["extension_id"] == ""
    assert dumped["title"] == ""
    assert dumped["seq"] == 0


@pytest.mark.parametrize("state", ["pending", "approved", "dismissed", ""])
def test_a_plan_turn_carries_each_known_state(state):
    assert PlanTurn(state=state).model_dump()["state"] == state


def test_a_plan_turn_defaults_to_the_empty_state_and_refuses_another():
    assert PlanTurn().state == ""
    with pytest.raises(ValidationError):
        PlanTurn(state="maybe")


def test_a_plan_turn_keeps_its_tasks_as_json():
    tasks = [{"id": 1, "done": False, "sub": [None, "x"]}]
    assert PlanTurn(tasks=tasks).model_dump()["tasks"] == tasks


def test_the_wire_json_fields_publish_as_any_json_value():
    schema = PlanTurn.model_json_schema()["properties"]["tasks"]["items"]
    assert schema["description"] == "Any JSON value."
    assert "object" in schema["type"]


def test_every_turn_field_has_a_description_for_sdk_developers():
    for cls in (
        UserTurn,
        AssistantTurn,
        ThinkingTurn,
        ToolTurn,
        PlanTurn,
        StatsTurn,
        VisualizationTurn,
        ChatSearchHit,
    ):
        for name, prop in cls.model_json_schema()["properties"].items():
            assert prop.get("description"), f"{cls.__name__}.{name}"


@pytest.mark.parametrize(
    ("cls", "role"), [*_CONVERSATIONAL, (StatsTurn, "stats"), (VisualizationTurn, "visualization")]
)
def test_a_dict_with_a_known_role_becomes_that_turn(cls, role):
    turn = _CHAT_TURN.validate_python({"role": role, "run_id": "r"})
    assert type(turn) is cls


@pytest.mark.parametrize("payload", [{"role": "system"}, {"run_id": "r"}])
def test_a_dict_with_an_unknown_or_missing_role_is_refused(payload):
    with pytest.raises(ValidationError):
        _CHAT_TURN.validate_python(payload)


def test_a_history_entry_keeps_what_every_turn_kind_dumped():
    turns = [
        UserTurn(run_id="r", created_at=1, content="héllo ✓"),
        AssistantTurn(content="a"),
        ThinkingTurn(content="t"),
        ToolTurn(tool_name="shell", args="ls", content="out"),
        PlanTurn(plan="p", state="approved"),
        StatsTurn(input_tokens=3, output_tokens=4, reasoning_tokens=1, duration=1.5),
        VisualizationTurn(spec_id="s", title="T", extension_id="e", seq=2),
    ]
    for turn in turns:
        dumped = turn.model_dump(mode="json")
        entry = ChatHistoryEntry.model_validate(dumped)
        assert {**entry.model_dump(), **dumped} == entry.model_dump()
    entry = ChatHistoryEntry.model_validate(turns[3].model_dump())
    assert (entry.tool_name, entry.args) == ("shell", "ls")


def test_a_history_entry_defaults_every_field():
    entry = ChatHistoryEntry()
    assert (entry.role, entry.run_id, entry.content, entry.created_at) == ("", "", "", 0)


def test_a_search_hit_defaults_to_zero_and_empty_and_keeps_snippet_offsets():
    assert ChatSearchHit().model_dump() == {
        "history_index": 0,
        "role": "",
        "run_id": "",
        "snippet": "",
        "match_start": 0,
        "match_end": 0,
        "created_at": 0,
    }
    hit = ChatSearchHit(
        history_index=4,
        role="user",
        run_id="r",
        snippet="une ✓ clé",
        match_start=4,
        match_end=5,
        created_at=9,
    )
    assert hit.snippet[hit.match_start : hit.match_end] == "✓"


def test_the_session_list_entry_is_the_protocol_s_own():
    assert sh.SessionListEntry is msg.SessionListEntry


def _all_none_message():
    return SimpleNamespace(
        role=None,
        content=None,
        created_at=None,
        from_history=None,
        tool_name=None,
        tool_call_id=None,
        tool_args=None,
        tool_call_error=None,
        reasoning_content=None,
        tool_calls=None,
    )


def test_a_message_whose_fields_are_all_none_reads_as_empty_values():
    view = AgnoRunMessageView.model_validate(_all_none_message(), from_attributes=True)
    assert view.role == ""
    assert view.created_at == 0
    assert view.from_history is False
    assert view.tool_call_error is False
    assert view.tool_name == view.tool_call_id == view.reasoning_content == ""
    assert view.tool_calls == []
    assert view.tool_args is None


def test_a_message_ignores_attributes_it_does_not_know():
    raw = SimpleNamespace(role="user", content="hi", brand_new_agno_field=1)
    view = AgnoRunMessageView.model_validate(raw, from_attributes=True)
    assert view.role == "user"
    assert not hasattr(view, "brand_new_agno_field")


def test_a_message_with_no_attributes_at_all_takes_the_defaults():
    view = AgnoRunMessageView.model_validate(SimpleNamespace(), from_attributes=True)
    assert (view.role, view.content, view.created_at) == ("", "", 0)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("text", "text"),
        ("", ""),
        (None, ""),
        (0, ""),
        ([], ""),
        (42, "42"),
        ([{"type": "text", "text": "x"}], "[{'type': 'text', 'text': 'x'}]"),
    ],
)
def test_content_str_keeps_a_string_and_stringifies_the_rest(content, expected):
    assert AgnoRunMessageView(content=content).content_str == expected


def test_a_malformed_tool_call_entry_is_refused():
    with pytest.raises(ValidationError):
        AgnoRunMessageView(tool_calls=["not a dict"])


def test_a_wrong_typed_flag_is_refused():
    with pytest.raises(ValidationError):
        AgnoRunMessageView(tool_call_error="sometimes")


def test_metrics_with_none_members_read_as_zero():
    view = AgnoRunMetricsView.model_validate(
        SimpleNamespace(reasoning_tokens=None, duration=None), from_attributes=True
    )
    assert (view.reasoning_tokens, view.duration) == (0, 0.0)


def test_metrics_keep_their_values_and_ignore_extras():
    view = AgnoRunMetricsView(reasoning_tokens=5, duration=2.5, input_tokens=9)
    assert (view.reasoning_tokens, view.duration) == (5, 2.5)


def test_a_run_with_none_ids_and_messages_reads_as_empty():
    run = AgnoRunView.model_validate(
        SimpleNamespace(run_id=None, parent_run_id=None, messages=None, metrics=None),
        from_attributes=True,
    )
    assert (run.run_id, run.parent_run_id, run.messages, run.metrics) == ("", "", [], None)


def test_a_run_id_that_is_not_a_string_is_stringified():
    uid = uuid.uuid4()
    run = AgnoRunView(run_id=uid, parent_run_id=17)
    assert (run.run_id, run.parent_run_id) == (str(uid), "17")


@pytest.mark.parametrize(
    "metrics",
    [
        {"reasoning_tokens": 3, "duration": 1.25},
        SimpleNamespace(reasoning_tokens=3, duration=1.25, other="x"),
    ],
)
def test_a_run_accepts_metrics_as_a_dict_or_an_object(metrics):
    run = AgnoRunView(metrics=metrics)
    assert (run.metrics.reasoning_tokens, run.metrics.duration) == (3, 1.25)


def test_a_run_keeps_its_messages_unvalidated():
    marker = object()
    assert AgnoRunView(messages=[marker]).messages == [marker]
