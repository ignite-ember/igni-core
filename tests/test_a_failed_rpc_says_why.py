"""The code a failed RPC answers with, so a client can branch on it.

``code_of`` is the one table behind every route that answers an RPC. The
order of its branches is the contract: ``RpcArgsError`` is a
``ValueError`` and ``KeyError`` is a ``LookupError``, and each must keep
the more specific answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.backend.message_dispatcher import MessageDispatcher
from igni_core.backend.rpc_binding import NoArgs, RpcArgsError
from igni_core.backend.rpc_errors import code_of
from igni_core.backend.run_controller import RunCancelledError
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.rpc import RpcErrorCode


def _args_error() -> RpcArgsError:
    try:
        NoArgs.model_validate({"x": 1})
    except ValidationError as exc:
        return RpcArgsError.of(RpcMethod.START_SCHEDULER, exc, {"x": 1})
    raise AssertionError("NoArgs accepted a key")


class _Cancelled(RunCancelledError):
    pass


CASES = [
    pytest.param(_args_error(), RpcErrorCode.INVALID, id="args-not-fitting"),
    pytest.param(KeyError("k"), RpcErrorCode.INVALID, id="missing-key"),
    pytest.param(RunCancelledError("stop"), RpcErrorCode.CANCELLED, id="cancelled"),
    pytest.param(_Cancelled("stop"), RpcErrorCode.CANCELLED, id="cancelled-subclass"),
    pytest.param(FileNotFoundError("gone"), RpcErrorCode.NOT_FOUND, id="no-file"),
    pytest.param(IndexError("0"), RpcErrorCode.NOT_FOUND, id="index"),
    pytest.param(LookupError("x"), RpcErrorCode.NOT_FOUND, id="lookup"),
    pytest.param(ValueError("no"), RpcErrorCode.REFUSED, id="refusal"),
    pytest.param(
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), RpcErrorCode.REFUSED, id="unicode"
    ),
    pytest.param(PermissionError("no"), RpcErrorCode.REFUSED, id="permission"),
    pytest.param(RuntimeError("boom"), RpcErrorCode.FAILED, id="runtime"),
    pytest.param(OSError("disk"), RpcErrorCode.FAILED, id="os"),
    pytest.param(BaseException("odd"), RpcErrorCode.FAILED, id="base"),
]


@pytest.mark.parametrize(("exc", "code"), CASES)
def test_an_exception_maps_to_its_code(exc: BaseException, code: RpcErrorCode):
    assert code_of(exc) is code


def test_the_code_is_the_wire_word():
    assert code_of(ValueError("x")) == "refused"


class _Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


def _dispatcher(transport: _Transport, table: dict) -> MessageDispatcher:
    dispatcher = MessageDispatcher.__new__(MessageDispatcher)
    dispatcher._transport = transport  # type: ignore[attr-defined]
    dispatcher._rpc_table = table  # type: ignore[attr-defined]
    return dispatcher


def _raiser(exc: BaseException):
    def handler(_args: dict) -> None:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        pytest.param(ValueError("no"), RpcErrorCode.REFUSED, id="refusal"),
        pytest.param(FileNotFoundError("gone"), RpcErrorCode.NOT_FOUND, id="no-file"),
        pytest.param(PermissionError("no"), RpcErrorCode.REFUSED, id="permission"),
        pytest.param(RunCancelledError("stop"), RpcErrorCode.CANCELLED, id="cancelled"),
        pytest.param(RuntimeError("boom"), RpcErrorCode.FAILED, id="runtime"),
        pytest.param(_args_error(), RpcErrorCode.INVALID, id="args"),
    ],
)
async def test_the_dispatcher_answers_a_failed_call_with_the_code(
    exc: BaseException, code: RpcErrorCode
):
    transport = _Transport()
    dispatcher = _dispatcher(transport, {"m": _raiser(exc)})

    await dispatcher._on_rpc_request(msg.RPCRequest(method="m", args={}), "req-1")

    [reply] = transport.sent
    assert reply.code == code
    assert reply.error == str(exc)
    assert reply.result is None
