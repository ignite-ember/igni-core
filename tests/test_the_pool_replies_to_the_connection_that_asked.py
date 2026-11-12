"""``PoolReplier``: who an answer or a refusal reaches, and in what shape.

A reply is addressed to the connection that sent the request (an answer can
carry a service token); a request from the terminal has no connection and its
answer goes to everyone. A refusal takes the shape its sender waits for: an
RPC its error, a slash command an error result, anything else (an approval) an
``Error`` and the end of the stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel

from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.schemas_pool import PoolAnswer
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import Message
from igni_core.protocol.schemas.enums import CommandResultKind
from igni_core.protocol.schemas.rpc import RpcErrorCode


class _Wire:
    def __init__(self) -> None:
        self.sent: list[Message] = []
        self.heard: list[Message] = []
        self.fail = False

    async def send(self, message: Message) -> None:
        if self.fail:
            raise ConnectionError("window gone")
        self.sent.append(message)

    async def receive(self) -> AsyncIterator[Message]:
        for m in self.heard:
            yield m


class _Payload(BaseModel):
    token: str


def _from(message: Message, client: str) -> Message:
    if client:
        message.mark_from(client)
    return message


def _rpc(client: str = "w1") -> msg.RPCRequest:
    return _from(msg.RPCRequest(id="r1", method="x"), client)  # type: ignore[return-value]


async def test_an_answer_goes_back_to_the_connection_that_asked_and_to_no_other():
    wire = _Wire()

    await PoolReplier(wire).reply(_rpc("w1"), PoolAnswer.ok(_Payload(token="secret")))

    (sent,) = wire.sent
    assert isinstance(sent, msg.RPCResponse)
    assert (sent.id, sent.result, sent.error) == ("r1", {"token": "secret"}, None)
    assert sent.to_client == "w1"


async def test_a_request_from_the_terminal_has_its_answer_unaddressed():
    wire = _Wire()

    await PoolReplier(wire).reply(_rpc(""), PoolAnswer.ok(_Payload(token="t")))

    assert wire.sent[0].to_client == ""


async def test_an_answer_without_a_result_is_a_success_with_no_payload():
    wire = _Wire()

    await PoolReplier(wire).reply(_rpc(), PoolAnswer())

    (sent,) = wire.sent
    assert isinstance(sent, msg.RPCResponse)
    assert (sent.result, sent.error, sent.code) == (None, None, None)


async def test_an_answer_dumps_its_result_in_the_mode_it_names():
    from enum import StrEnum

    class _Kind(StrEnum):
        A = "a"

    class _Typed(BaseModel):
        kind: _Kind

    wire = _Wire()
    replier = PoolReplier(wire)
    await replier.reply(_rpc(), PoolAnswer.ok(_Typed(kind=_Kind.A), dump_mode="json"))

    assert wire.sent[0].result == {"kind": "a"}  # type: ignore[attr-defined]


async def test_a_failed_answer_carries_its_reason_and_code():
    wire = _Wire()

    await PoolReplier(wire).reply(
        _rpc(), PoolAnswer.failed("no such thing", RpcErrorCode.NOT_FOUND)
    )

    (sent,) = wire.sent
    assert (sent.result, sent.error, sent.code) == (None, "no such thing", RpcErrorCode.NOT_FOUND)  # type: ignore[attr-defined]


async def test_a_failure_with_no_code_is_reported_as_failed():
    wire = _Wire()

    await PoolReplier(wire).reply(_rpc(), PoolAnswer(error="went wrong"))

    assert wire.sent[0].code == RpcErrorCode.FAILED  # type: ignore[attr-defined]


async def test_an_empty_error_is_a_success_not_a_failure():
    wire = _Wire()

    await PoolReplier(wire).reply(_rpc(), PoolAnswer(error="", result=_Payload(token="t")))

    assert wire.sent[0].error is None  # type: ignore[attr-defined]
    assert wire.sent[0].result == {"token": "t"}  # type: ignore[attr-defined]


async def test_a_refused_rpc_gets_forbidden_with_the_reason():
    wire = _Wire()

    await PoolReplier(wire).refuse(_rpc("w2"), "not for you")

    (sent,) = wire.sent
    assert isinstance(sent, msg.RPCResponse)
    assert (sent.id, sent.error, sent.code) == ("r1", "not for you", RpcErrorCode.FORBIDDEN)
    assert sent.to_client == "w2"


async def test_a_refused_slash_command_gets_an_error_result_with_the_reason():
    wire = _Wire()
    command = _from(msg.Command(id="c1", text="/x"), "w3")

    await PoolReplier(wire).refuse(command, "read-only window")

    (sent,) = wire.sent
    assert isinstance(sent, msg.CommandResult)
    assert (sent.id, sent.kind, sent.content) == ("c1", CommandResultKind.ERROR, "read-only window")
    assert sent.to_client == "w3"


async def test_a_refused_approval_gets_an_error_then_the_end_of_its_stream():
    wire = _Wire()
    approval = _from(msg.HITLResponseBatch(id="a1", session_id="s1"), "w4")

    await PoolReplier(wire).refuse(approval, "not allowed")

    error, end = wire.sent
    assert isinstance(error, msg.Error) and isinstance(end, msg.StreamEnd)
    assert (error.id, error.session_id, error.text) == ("a1", "s1", "not allowed")
    assert (end.id, end.session_id) == ("a1", "s1")
    assert error.to_client == end.to_client == "w4"


async def test_a_broadcast_is_sent_to_every_window_unaddressed():
    wire = _Wire()
    note = msg.Info(text="hello")

    await PoolReplier(wire).broadcast(note)

    assert wire.sent == [note]
    assert note.to_client == ""


async def test_what_the_windows_send_is_what_the_serve_loop_hears():
    wire = _Wire()
    wire.heard = [msg.Info(text="1"), msg.Info(text="2")]

    heard = [m async for m in PoolReplier(wire).incoming()]

    assert heard == wire.heard


async def test_a_transport_that_fails_to_send_is_not_hidden():
    wire = _Wire()
    wire.fail = True

    with pytest.raises(ConnectionError):
        await PoolReplier(wire).reply(_rpc(), PoolAnswer())
