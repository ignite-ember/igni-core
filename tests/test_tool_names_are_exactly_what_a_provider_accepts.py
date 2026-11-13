"""The shape of a provider-safe tool name, at its edges.

Complements the end-to-end tests of what each provider is sent: here the
pure rules are pinned where they are easiest to get wrong, and the
boundary's rewriting is driven through a real mixin over a recording base.
"""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.config.tool_names import (
    MAX_LENGTH,
    VALID_NAME,
    ProviderToolNames,
    ToolNameBoundary,
    wire_name,
)

EXTENSION = "mcp_ext:acme.tickets_lookup_ticket"
EXTENSION_WIRE = "mcp_ext_acme_tickets_lookup_ticket"


def test_a_name_of_exactly_64_characters_is_kept_and_65_is_hashed() -> None:
    exact = "a" * 64
    over = "a" * 65

    assert wire_name(exact) == exact
    hashed = wire_name(over)
    assert len(hashed) == 64
    assert hashed != over


def test_a_hashed_name_is_55_characters_an_underscore_and_8_hex_of_the_original() -> None:
    name = "mcp_ext:" + "x" * 80

    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    assert wire_name(name) == "mcp_ext_" + "x" * 47 + "_" + digest
    assert len(wire_name(name)) == MAX_LENGTH


def test_two_long_names_sharing_a_prefix_get_distinct_wire_names() -> None:
    first = "mcp_ext:" + "x" * 80 + "one"
    second = "mcp_ext:" + "x" * 80 + "two"

    assert wire_name(first) != wire_name(second)
    assert wire_name(first)[:55] == wire_name(second)[:55]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", "_"),
        ("::", "__"),
        ("-dash", "_-dash"),
        ("9lives", "_9lives"),
        ("naïve.tool", "na_ve_tool"),
        ("日本", "__"),
        ("with space", "with_space"),
    ],
)
def test_a_name_a_provider_would_refuse_is_made_valid(name: str, expected: str) -> None:
    assert wire_name(name) == expected
    assert VALID_NAME.fullmatch(wire_name(name))


@pytest.mark.parametrize("name", ["tool\n", "tool\n\n", "\n", "a\nb"])
def test_a_name_with_a_newline_never_reaches_a_provider(name: str) -> None:
    sent = wire_name(name)

    assert "\n" not in sent
    assert re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}", sent)


def test_assign_hashes_the_one_that_collides_and_keeps_the_valid_one() -> None:
    names = ProviderToolNames()

    mapping = names.assign([EXTENSION, EXTENSION_WIRE])

    assert mapping[EXTENSION_WIRE] == EXTENSION_WIRE
    assert mapping[EXTENSION] != EXTENSION_WIRE
    assert VALID_NAME.fullmatch(mapping[EXTENSION])
    assert names.registered(mapping[EXTENSION]) == EXTENSION


def test_assign_ignores_duplicates_and_the_order_names_come_in() -> None:
    forward = ProviderToolNames().assign(["a:b", "a.b", "a:b"])
    backward = ProviderToolNames().assign(["a.b", "a:b"])

    assert forward == backward
    assert len(set(forward.values())) == 2


def test_assign_across_requests_still_maps_the_earlier_names_back() -> None:
    names = ProviderToolNames()
    first = names.assign([EXTENSION])
    names.assign(["mcp_ext:other.tool"])

    assert names.registered(first[EXTENSION]) == EXTENSION
    assert names.registered("mcp_ext_other_tool") == "mcp_ext:other.tool"


# ── The boundary, over a recording base ───────────────────────────────


class _Base:
    def __init__(self, reply: Any = None) -> None:
        self.reply = reply
        self.seen: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        self.seen = (args, kwargs)
        return self.reply

    def parse_tool_calls(self, data: Any) -> Any:
        return data


class _Model(ToolNameBoundary, _Base):
    pass


class _Message:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)

    def model_copy(self, update: dict[str, Any]) -> _Message:
        return _Message(**{**self.__dict__, **update})


def _definition(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_a_request_with_nothing_to_rename_reaches_the_provider_untouched() -> None:
    model = _Model()
    messages = [_Message(tool_calls=None, tool_name=None)]
    tools = [_definition("read_file")]

    model.invoke(messages, tools=tools, tool_choice="auto")

    args, kwargs = model.seen  # type: ignore[misc]
    assert args[0] is messages
    assert kwargs["tools"] is tools
    assert kwargs["tool_choice"] == "auto"


def test_tools_choice_and_history_are_renamed_with_messages_positional() -> None:
    model = _Model()
    history = [
        _Message(
            tool_calls=[
                {"id": "1", "function": {"name": EXTENSION, "arguments": "{}"}},
                SimpleNamespace(function=SimpleNamespace(name="read_file")),
            ],
            tool_name=None,
        ),
        _Message(tool_calls=None, tool_name=EXTENSION),
        _Message(tool_calls=None, tool_name=7),
    ]

    model.invoke(
        history,
        tools=[_definition(EXTENSION), {"name": "read_file"}],
        tool_choice={"type": "function", "function": {"name": EXTENSION}},
    )

    args, kwargs = model.seen  # type: ignore[misc]
    assert kwargs["tools"][0]["function"]["name"] == EXTENSION_WIRE
    assert kwargs["tools"][1] == {"name": "read_file"}
    assert kwargs["tool_choice"]["function"]["name"] == EXTENSION_WIRE
    assert args[0][0].tool_calls[0]["function"]["name"] == EXTENSION_WIRE
    assert args[0][0].tool_calls[1].function.name == "read_file"
    assert args[0][1].tool_name == EXTENSION_WIRE
    assert args[0][2].tool_name == 7
    assert history[1].tool_name == EXTENSION  # the caller's history is not edited


def test_messages_passed_by_keyword_are_renamed_by_keyword() -> None:
    model = _Model()

    model.invoke(messages=[_Message(tool_calls=None, tool_name=EXTENSION)], tools=[])

    args, kwargs = model.seen  # type: ignore[misc]
    assert args == ()
    assert kwargs["messages"][0].tool_name == EXTENSION_WIRE


def test_a_reply_is_mapped_back_for_dict_and_object_calls() -> None:
    model = _Model()
    model.invoke([], tools=[_definition(EXTENSION)])
    as_dict = {"function": {"name": EXTENSION_WIRE}}
    as_object = SimpleNamespace(function=SimpleNamespace(name=EXTENSION_WIRE))
    unknown = {"function": {"name": "made_up"}}

    parsed = model.parse_tool_calls([as_dict, as_object, unknown])

    assert as_dict["function"]["name"] == EXTENSION
    assert as_object.function.name == EXTENSION
    assert unknown["function"]["name"] == "made_up"
    assert parsed[0] is as_dict


def test_a_frozen_call_keeps_its_wire_name_and_a_missing_reply_is_tolerated() -> None:
    model = _Model()
    model.invoke([], tools=[_definition(EXTENSION)])

    class Frozen:
        __slots__ = ("_n",)

        def __init__(self, n: str) -> None:
            self._n = n

        @property
        def name(self) -> str:
            return self._n

    frozen = SimpleNamespace(function=Frozen(EXTENSION_WIRE))

    assert model.parse_tool_calls([frozen])[0].function.name == EXTENSION_WIRE
    assert model.parse_tool_calls(None) is None
    assert model.parse_tool_calls([]) == []
    assert model._from_wire(SimpleNamespace(tool_calls=None)).tool_calls is None
