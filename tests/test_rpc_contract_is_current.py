"""The RPC contract igni-sdk generates its client from is complete and current.

``docs/rpc-contract.json`` is built from the methods that implement each
RPC (``protocol/rpc_contract.py``); nothing in it is written by hand. These
tests regenerate it and hold the committed copy to what the code says, and
hold the contract to its promise: every method has typed arguments, a
typed result and documentation, and nothing in it is "any value".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from igni_core.protocol import rpc_contract
from igni_core.protocol.rpc import RpcAccess, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.envelope import Direction

REGENERATE = "make rpc-contract"

#: Keys that describe a schema without constraining it.
_ANNOTATIONS = frozenset({"title", "description", "default", "examples", "deprecated"})


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return rpc_contract.build()


def test_the_published_file_is_what_the_code_says(contract: dict[str, Any]) -> None:
    """A method, argument, result, docstring or message changed and the
    file was not regenerated."""
    assert rpc_contract.SNAPSHOT_PATH.read_text() == rpc_contract.render(contract), (
        f"{rpc_contract.SNAPSHOT_PATH.name} is out of date with the code. Run:\n    {REGENERATE}"
    )


def test_the_published_file_is_json_with_every_part() -> None:
    data = json.loads(rpc_contract.SNAPSHOT_PATH.read_text())

    assert data["version"] == rpc_contract.VERSION
    assert data["defs"] and data["methods"] and data["messages"]


def test_every_method_is_in_the_contract(contract: dict[str, Any]) -> None:
    assert set(contract["methods"]) == {m.value for m in RpcMethod}


@pytest.mark.parametrize("method", sorted(m.value for m in RpcMethod))
def test_every_method_is_described(contract: dict[str, Any], method: str) -> None:
    entry = contract["methods"][method]

    assert entry["args"], f"{method} has no arguments schema"
    assert entry["result"], f"{method} has no result schema"
    assert entry["summary"].strip(), f"{method} has no summary: give its method a docstring"
    assert entry["doc"].startswith(entry["summary"].split(" ")[0])
    assert entry["scope"] in {s.value for s in RpcScope}
    assert entry["access"] in {a.value for a in RpcAccess}
    assert entry["reply"] in {"rpc_response", "message"}
    assert entry["area"]


@pytest.mark.parametrize("method", sorted(m.value for m in RpcMethod))
def test_no_method_takes_or_answers_any_value(contract: dict[str, Any], method: str) -> None:
    """``{}`` and ``additionalProperties: true`` say "anything": the SDK
    could type nothing from them. A value whose shape the protocol does
    not fix is ``JsonValue`` — any JSON, said as such."""
    entry = contract["methods"][method]
    for part in ("args", "result"):
        loose = list(_unconstrained(entry[part], contract["defs"], f"{method}.{part}"))
        assert not loose, f"{method}'s {part} is not typed at: {loose}"


@pytest.mark.parametrize(
    "message", sorted(rpc_contract.message_classes(), key=lambda c: c.__name__)
)
def test_no_message_carries_any_value(contract: dict[str, Any], message: type) -> None:
    wire = rpc_contract._wire_type(message)
    entry = contract["messages"][wire]
    loose = list(_unconstrained(entry["schema"], contract["defs"], wire))
    assert not loose, f"{wire} is not typed at: {loose}"


def test_every_stream_is_a_message_or_a_typed_push(contract: dict[str, Any]) -> None:
    """A stream names a message the contract describes, or a push channel
    whose payload it describes."""
    messages = set(contract["messages"])
    channels = {f"push_notification:{c}" for c in contract["push_channels"]}
    for method, entry in contract["methods"].items():
        unknown = set(entry["streams"]) - messages - channels
        assert not unknown, f"{method} streams what the contract does not describe: {unknown}"


def test_every_push_channel_has_a_payload(contract: dict[str, Any]) -> None:
    assert set(contract["push_channels"]) == {c.value for c in PushChannel} - {"unknown"}


@pytest.mark.parametrize("channel", sorted(c.value for c in PushChannel if c.value != "unknown"))
def test_no_push_payload_carries_any_value(contract: dict[str, Any], channel: str) -> None:
    entry = contract["push_channels"][channel]
    assert entry["doc"].strip(), f"{channel} has no doc"
    loose = list(_unconstrained(entry["schema"], contract["defs"], channel))
    assert not loose, f"{channel}'s payload is not typed at: {loose}"


def test_every_broadcast_event_is_its_channel_s_payload() -> None:
    """A broadcast-bus event names its channel; the table must agree, or the
    contract describes a payload nobody sends."""
    import typing

    import igni_core.backend.rpc_router  # noqa: F401  (imports every event's module)
    from igni_core.backend.push_payloads import PUSH_PAYLOADS
    from igni_core.core.broadcast_schema import BroadcastEvent, RunScopedBroadcastEvent

    def concrete(cls: type) -> list[type]:
        found = []
        for sub in cls.__subclasses__():
            found.extend(concrete(sub))
            if sub is not RunScopedBroadcastEvent and not sub.__name__.startswith("_"):
                found.append(sub)
        return found

    for event in concrete(BroadcastEvent):
        if event.__module__.startswith("tests"):
            continue
        channel = PushChannel(event.model_fields["channel"].default)
        assert channel is not PushChannel.UNKNOWN, f"{event.__name__} names no known channel"
        model = PUSH_PAYLOADS[channel].model
        assert event in (typing.get_args(model) or (model,)), (
            f"{event.__name__} is sent on {channel.value} but is not its payload model"
        )


def test_every_message_says_which_way_it_travels(contract: dict[str, Any]) -> None:
    for wire, entry in contract["messages"].items():
        assert entry["direction"] in {d.value for d in Direction}, wire
        assert entry["doc"].strip(), f"{wire} has no docstring"


def test_a_message_result_is_answered_as_that_message(contract: dict[str, Any]) -> None:
    """igni answers a method whose result is a message with the message
    itself; the contract says so, so a client reads the reply right."""
    assert contract["methods"]["get_status"]["reply"] == "message"
    assert contract["methods"]["get_session_id"]["reply"] == "rpc_response"


def _unconstrained(schema: Any, defs: dict[str, Any], where: str) -> Iterator[str]:
    """Every place under ``schema`` (following ``$ref`` into ``defs``) that
    admits any value."""
    seen: set[str] = set()

    def walk(node: Any, at: str) -> Iterator[str]:
        if node is True:
            yield at
            return
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name not in seen:
                seen.add(name)
                yield from walk(defs[name], f"#/defs/{name}")
            return
        if not set(node) - _ANNOTATIONS:
            yield at
            return
        if node.get("additionalProperties") is True:
            yield f"{at}.additionalProperties"
        for key in ("properties", "patternProperties"):
            for name, sub in (node.get(key) or {}).items():
                yield from walk(sub, f"{at}.{name}")
        for key in ("items", "additionalProperties", "not"):
            if isinstance(node.get(key), dict | bool) and node.get(key) is not False:
                if key == "additionalProperties" and node.get(key) is True:
                    continue
                yield from walk(node[key], f"{at}[{key}]")
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for i, sub in enumerate(node.get(key) or []):
                yield from walk(sub, f"{at}.{key}[{i}]")

    yield from walk(schema, where)


def test_the_contract_says_who_reaches_each_access_level(contract: dict[str, Any]) -> None:
    for level in RpcAccess:
        assert f"``{level.value}``" in contract["access"], f"{level.value} is not explained"
