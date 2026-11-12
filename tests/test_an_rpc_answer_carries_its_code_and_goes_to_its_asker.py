"""An RPC's answer: the code it fails with, and the one connection it goes to.

:class:`MessageDispatcher` answers every ``rpc_request``. Pinned here, on
the wire message a client receives:

* a failure carries a machine-readable code: ``UNKNOWN_METHOD`` for a
  method nobody serves, ``INVALID`` for a missing argument, the code of
  the exception otherwise, ``UNAVAILABLE`` for an ``ext.*`` call on a
  backend with no extension support, ``FORBIDDEN`` for a refused one;
* every answer, result or failure, base or extension, is addressed to the
  connection that asked and to no other, and a request from a transport
  without connections is answered as a broadcast;
* ``ext.*`` is routed to the extensions even when the base table has a
  handler of that name.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.backend.message_dispatcher import MessageDispatcher
from igni_core.extensions.registry import ExtensionRefused
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.rpc import RpcErrorCode


class _Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _Extensions:
    def __init__(self, outcome: Any = None) -> None:
        self._outcome = outcome

    async def dispatch(self, *, caller: Any, method: str, args: dict) -> Any:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _dispatcher(
    table: dict | None = None, extensions: Any = None
) -> tuple[MessageDispatcher, _Transport]:
    transport = _Transport()
    dispatcher = MessageDispatcher(
        backend=None,  # type: ignore[arg-type]
        transport=transport,
        rpc_table=table or {},
        queue=[],
        login=None,
        extensions=extensions,
    )
    return dispatcher, transport


def _request(method: str, *, from_client: str = "", **args: Any) -> msg.RPCRequest:
    request = msg.RPCRequest(id="q", method=method, args=args, caller="acme")
    if from_client:
        request.mark_from(from_client)
    return request


# ── Codes ─────────────────────────────────────────────────────────


async def test_a_method_nobody_serves_fails_with_unknown_method():
    dispatcher, transport = _dispatcher()

    await dispatcher.dispatch(_request("nope"))

    (reply,) = transport.sent
    assert reply.code == RpcErrorCode.UNKNOWN_METHOD
    assert reply.error == "Unknown RPC method: nope"
    assert reply.id == "q"


async def test_a_missing_argument_fails_with_invalid_and_names_the_method_and_key():
    dispatcher, transport = _dispatcher({"get": lambda args: args["session_id"]})

    await dispatcher.dispatch(_request("get"))

    (reply,) = transport.sent
    assert reply.code == RpcErrorCode.INVALID
    assert reply.error == "get: 'session_id' is a required argument"
    assert reply.id == "q"


@pytest.mark.parametrize(
    "exc,code",
    [
        (ValueError("no"), RpcErrorCode.REFUSED),
        (PermissionError("denied"), RpcErrorCode.REFUSED),
        (FileNotFoundError("gone"), RpcErrorCode.NOT_FOUND),
        (RuntimeError("broke"), RpcErrorCode.FAILED),
    ],
)
async def test_any_other_failure_carries_its_text_and_the_code_of_its_kind(exc, code):
    def handler(args: dict) -> None:
        raise exc

    dispatcher, transport = _dispatcher({"m": handler})

    await dispatcher.dispatch(_request("m"))

    (reply,) = transport.sent
    assert (reply.error, reply.code) == (str(exc), code)


async def test_an_extension_call_on_a_backend_without_extensions_is_unavailable():
    dispatcher, transport = _dispatcher(extensions=None)

    await dispatcher.dispatch(_request("ext.acme.go"))

    (reply,) = transport.sent
    assert reply.code == RpcErrorCode.UNAVAILABLE
    assert reply.error.startswith("ext.acme.go: this backend has no extension support")


async def test_a_refused_extension_call_is_forbidden_and_logged_at_info(caplog):
    dispatcher, transport = _dispatcher(extensions=_Extensions(ExtensionRefused("not yours")))

    with caplog.at_level("INFO"):
        await dispatcher.dispatch(_request("ext.acme.go"))

    (reply,) = transport.sent
    assert (reply.code, reply.error) == (RpcErrorCode.FORBIDDEN, "not yours")
    record = next(r for r in caplog.records if "refused" in r.getMessage())
    assert record.levelname == "INFO"
    assert record.exc_info is None


async def test_a_broken_extension_call_carries_the_code_of_its_failure():
    dispatcher, transport = _dispatcher(extensions=_Extensions(FileNotFoundError("no file")))

    await dispatcher.dispatch(_request("ext.acme.go"))

    (reply,) = transport.sent
    assert (reply.code, reply.error) == (RpcErrorCode.NOT_FOUND, "no file")


async def test_an_extension_name_in_the_base_table_is_never_served_from_it():
    served: list[dict] = []
    dispatcher, transport = _dispatcher(
        {"ext.acme.go": lambda args: served.append(args)},
        extensions=_Extensions({"from": "extension"}),
    )

    await dispatcher.dispatch(_request("ext.acme.go"))

    assert served == []
    assert transport.sent[0].result == {"from": "extension"}


# ── Addressing ────────────────────────────────────────────────────


async def test_every_kind_of_answer_goes_to_the_connection_that_asked():
    async def coroutine(args: dict) -> str:
        return "from a coroutine"

    table = {
        "ok": lambda args: {"v": 1},
        "wire": lambda args: msg.Info(text="as a message"),
        "late": coroutine,
        "bad": lambda args: (_ for _ in ()).throw(ValueError("refused")),
        "missing": lambda args: args["k"],
    }
    dispatcher, transport = _dispatcher(table, extensions=_Extensions({"x": 1}))

    for method in (*table, "unknown", "ext.acme.go"):
        await dispatcher.dispatch(_request(method, from_client="tab-7"))

    assert len(transport.sent) == len(table) + 2
    assert [m.to_client for m in transport.sent] == ["tab-7"] * len(transport.sent)


async def test_two_askers_each_get_only_their_own_answer():
    dispatcher, transport = _dispatcher({"m": lambda args: "x"})

    await dispatcher.dispatch(_request("m", from_client="tab-a"))
    await dispatcher.dispatch(_request("m", from_client="tab-b"))

    assert [m.to_client for m in transport.sent] == ["tab-a", "tab-b"]


async def test_a_request_from_a_transport_without_connections_is_answered_as_a_broadcast():
    dispatcher, transport = _dispatcher({"m": lambda args: "x"})

    await dispatcher.dispatch(_request("m"))

    assert transport.sent[0].to_client == ""
