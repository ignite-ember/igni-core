"""``SpawnArgs`` and ``TurnToolCall``: how a waiting spawn call's arguments and
its assistant turn are read back, whatever shape Agno stored them in."""

from __future__ import annotations

import pytest

from igni_core.core.tools.spawn_args import SpawnArgs, TurnToolCall


def test_a_none_is_empty_text_and_a_number_is_its_text():
    args = SpawnArgs.model_validate(
        {"task": None, "agent_name": 7, "agent_names": ["a", "b"], "mode": None, "isolation": 1.5}
    )

    assert args.task == ""
    assert args.agent_name == "7"
    assert args.agent_names == "['a', 'b']"
    assert args.mode == ""
    assert args.isolation == "1.5"


def test_nothing_given_reads_as_all_empty():
    args = SpawnArgs.model_validate({})

    assert (args.task, args.agent_name, args.agent_names, args.mode, args.isolation) == ("",) * 5


def test_keys_a_spawn_does_not_take_are_ignored():
    args = SpawnArgs.model_validate({"task": "t", "timeout": 30, "extra": {"x": 1}})

    assert args.task == "t"
    assert not hasattr(args, "timeout")


@pytest.mark.parametrize(
    ("raw", "names"),
    [
        ("a,b,c", ["a", "b", "c"]),
        (" a , b ,, c, ", ["a", "b", "c"]),
        ("solo", ["solo"]),
        ("", []),
        (" , ,", []),
        ("Zoë, 日本", ["Zoë", "日本"]),
    ],
)
def test_team_names_are_trimmed_and_empty_entries_dropped(raw, names):
    assert SpawnArgs(agent_names=raw).team_names == names


def test_the_label_is_the_agent_name_or_else_the_team_names():
    assert SpawnArgs(agent_name="coder", agent_names="a,b").agent_label == "coder"
    assert SpawnArgs(agent_name="", agent_names="a,b").agent_label == "a,b"
    assert SpawnArgs().agent_label == ""


def test_a_wire_call_is_read_into_id_name_and_arguments():
    call = TurnToolCall.from_wire(
        {"id": "call_1", "function": {"name": "spawn_agent", "arguments": '{"task": "x"}'}}
    )

    assert (call.id, call.name, call.arguments) == ("call_1", "spawn_agent", {"task": "x"})


@pytest.mark.parametrize(
    "wire",
    [
        {},
        {"id": None, "function": None},
        {"id": None, "function": {}},
        {"function": {"name": None, "arguments": None}},
    ],
)
def test_a_wire_call_missing_pieces_reads_as_empty(wire):
    call = TurnToolCall.from_wire(wire)

    assert (call.id, call.name, call.arguments) == ("", "", {})


def test_a_number_id_is_its_text():
    assert TurnToolCall.from_wire({"id": 12, "function": {"name": "t"}}).id == "12"


def test_dict_arguments_are_copied_not_shared():
    original = {"task": "x"}

    parsed = TurnToolCall.parse_arguments(original)
    parsed["task"] = "changed"

    assert original == {"task": "x"}


@pytest.mark.parametrize(
    "raw",
    ["{not json", "[1, 2]", "null", "3", '"text"', "", None, 5, b"[]", b"\xff", ["a"], object()],
)
def test_arguments_that_are_not_a_json_object_read_as_none(raw):
    assert TurnToolCall.parse_arguments(raw) == {}


def test_a_json_object_in_bytes_is_read():
    assert TurnToolCall.parse_arguments(b'{"a": 1}') == {"a": 1}
