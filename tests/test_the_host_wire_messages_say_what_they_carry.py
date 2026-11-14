"""The host model's wire messages: their shapes, defaults, refusals and flat hook value."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.wire import (
    PROTOCOL_VERSION,
    ErrorBody,
    Hello,
    HookAnswer,
    HookFrameValue,
    HookMessage,
    Op,
    Outcome,
    Reply,
    Request,
    SignalMessage,
)
from igni_core.extensions.principal import Principal


class Body(BaseModel):
    name: str
    note: str | None = None
    at: datetime | None = None


def test_the_protocol_is_version_one_and_hello_says_so() -> None:
    hello = Hello(schema_version="1.2.3", extension_id="acme.x", granted=[])

    assert PROTOCOL_VERSION == 1
    assert hello.model_dump(mode="json") == {
        "op": "hello",
        "protocol": 1,
        "schema_version": "1.2.3",
        "extension_id": "acme.x",
        "granted": [],
    }


def test_the_six_request_kinds_are_lowercase_words() -> None:
    assert {op.name: op.value for op in Op} == {
        "GET": "get",
        "SET": "set",
        "CALL": "call",
        "SUBSCRIBE": "subscribe",
        "UNSUBSCRIBE": "unsubscribe",
        "ANSWER": "answer",
    }


def test_a_request_needs_an_id_and_an_op_and_defaults_the_rest() -> None:
    request = Request.model_validate({"id": "r-1", "op": "get"})

    assert request.model_dump(mode="json") == {
        "id": "r-1",
        "op": "get",
        "path": "",
        "window": None,
        "session": None,
        "params": {},
        "value": None,
        "signals": [],
    }
    assert Request.model_validate({"id": 7, "op": "call"}).id == 7
    with pytest.raises(ValidationError):
        Request.model_validate({"op": "get"})
    with pytest.raises(ValidationError):
        Request.model_validate({"id": 1})


@pytest.mark.parametrize(
    "frame",
    [{"id": 1, "op": "get", "surprise": 1}, {"id": 1, "op": "delete"}],
    ids=["an unknown field", "an unknown op"],
)
def test_a_request_with_an_unknown_field_or_op_is_refused(frame: dict) -> None:
    with pytest.raises(ValidationError):
        Request.model_validate(frame)


def test_the_defaults_of_one_request_are_not_shared_with_the_next() -> None:
    first = Request(id=1, op=Op.CALL)
    first.params["a"] = 1
    first.signals.append("x")

    second = Request(id=2, op=Op.CALL)

    assert second.params == {}
    assert second.signals == []


def test_an_answer_carries_its_result_and_no_error() -> None:
    reply = Reply.answered("r-1", {"a": [1, 2]})

    assert reply.model_dump(mode="json") == {
        "op": "reply",
        "id": "r-1",
        "ok": True,
        "result": {"a": [1, 2]},
        "error": None,
    }
    assert Reply.answered(3).result is None


def test_a_refusal_carries_its_error_and_no_result() -> None:
    reply = Reply.refused(4, ErrorBody(code=ErrorCode.NOT_GRANTED, message="no"))

    assert reply.model_dump(mode="json") == {
        "op": "reply",
        "id": 4,
        "ok": False,
        "result": None,
        "error": {"code": ErrorCode.NOT_GRANTED.value, "message": "no"},
    }


def test_a_signal_is_told_with_its_name_and_value() -> None:
    assert SignalMessage(signal="chat.run_completed", value={"n": 1}).model_dump(mode="json") == {
        "op": "signal",
        "signal": "chat.run_completed",
        "value": {"n": 1},
    }


def test_a_hook_value_is_flat_with_the_manifest_name_beside_the_event_s_fields() -> None:
    value = HookFrameValue(body=Body(name="n"), tool_manifest_name="tool")

    assert value.model_dump(mode="json") == {
        "name": "n",
        "note": None,
        "at": None,
        "tool_manifest_name": "tool",
    }


def test_a_missing_manifest_name_is_left_out_but_an_empty_one_is_kept() -> None:
    assert "tool_manifest_name" not in HookFrameValue(body=Body(name="n")).model_dump(mode="json")
    assert (
        HookFrameValue(body=Body(name="n"), tool_manifest_name="").model_dump(mode="json")[
            "tool_manifest_name"
        ]
        == ""
    )


def test_a_told_event_leaves_out_what_is_not_set_and_a_decided_one_says_all() -> None:
    told = HookFrameValue(body=Body(name="n"), omit_none=True)
    decided = HookFrameValue(body=Body(name="n"), omit_none=False)

    assert told.model_dump(mode="json") == {"name": "n"}
    assert decided.model_dump(mode="json") == {"name": "n", "note": None, "at": None}


def test_a_hook_value_serialises_to_json_text() -> None:
    at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    message = HookMessage(event="E", value=HookFrameValue(body=Body(name="n", at=at)))

    assert json.loads(message.model_dump_json())["value"]["at"] == "2026-01-02T03:04:05Z"


def test_a_value_json_cannot_hold_is_told_as_its_text(tmp_path: Path) -> None:
    class Loose(BaseModel):
        model_config = {"arbitrary_types_allowed": True}
        path: Path
        tags: set[str]

    body = Loose.model_construct(path=tmp_path, tags={"a"})
    wire = HookFrameValue(body=body).model_dump(mode="json")
    json.dumps(wire)

    assert wire["path"] == str(tmp_path)
    assert wire["tags"] == ["a"]


def test_a_hook_that_decides_has_an_id_and_one_that_is_told_has_none() -> None:
    person = Principal(kind="person", id="u1", org="o")

    asked = HookMessage(id="h1", event="PreToolUse", principal=person)
    told = HookMessage(event="SessionStart")

    assert asked.model_dump(mode="json") == {
        "op": "hook",
        "id": "h1",
        "event": "PreToolUse",
        "value": None,
        "principal": person.model_dump(mode="json"),
    }
    assert asked.principal is not None
    assert asked.model_dump(mode="json")["principal"]["id"] == "u1"
    assert told.model_dump(mode="json") == {
        "op": "hook",
        "id": None,
        "event": "SessionStart",
        "value": None,
        "principal": None,
    }


def test_an_answer_names_its_hook_and_outcome_and_refuses_anything_else() -> None:
    answer = HookAnswer.model_validate({"hook": "h1", "outcome": "allow"})

    assert (answer.reason, answer.value, answer.outcome) == ("", None, Outcome.ALLOW)
    assert {o.value for o in Outcome} == {"allow", "reject", "modify", "failed"}
    for bad in (
        {"hook": "h1", "outcome": "maybe"},
        {"hook": "h1", "outcome": "allow", "extra": 1},
        {"outcome": "allow"},
        {"hook": "h1"},
    ):
        with pytest.raises(ValidationError):
            HookAnswer.model_validate(bad)


@pytest.mark.parametrize("value", [[1, "a"], "text", 3, True, {"a": {"b": [1]}}])
def test_an_answer_s_value_may_be_any_json(value: object) -> None:
    answer = HookAnswer.model_validate({"hook": "h", "outcome": "modify", "value": value})

    assert answer.value == value
