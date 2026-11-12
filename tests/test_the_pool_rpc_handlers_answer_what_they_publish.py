"""The pool's own calls answer what their published contract says, handler by handler.

Each handler under ``backend/pool_rpcs`` is reached through
:class:`PoolRpcRouter` and a real :class:`PoolReplier`, so what is asserted
is the ``rpc_response`` (and the messages after it) a window receives, on
the connection it asked from. The seams are the collaborators each
handler is given: the session router, the host model, the token
exchange, the credential keeper and the event inbox.

Pinned:

* the router starts with every slot answered, mounts replace, and
  ``handles`` is true only for a known method of an ``rpc_request``;
* ``POOL_RPCS`` lists the six handlers and every method they implement;
* a call that breaks is answered with its error and a code, a refusal
  keeps its own code, a non-model result is a ``TypeError``, and the
  result is dumped as JSON only where the call always was;
* ``attach_session`` refuses with ``refused``, replays waiting approvals
  after the answer with the request id, and survives a failing replay;
* ``window_model`` and ``host_model`` map the model's refusals to codes,
  let the connection's window win, and answer a malformed page request
  with a refused reply;
* ``extension_service_token``, ``vm_sign_in``, ``deliver_service_events``
  and the approver tokens say why when they cannot answer;
* the published contract declares the correlation, duration and streams
  each of these calls carries.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import BaseModel

from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.pool_rpcs import (
    POOL_RPCS,
    ApproverTokenRpc,
    AttachSessionRpc,
    HostModelRpcs,
    PageTokenRpc,
    PoolRefusal,
    PoolRpc,
    PoolRpcRouter,
    ServiceEventRpc,
    UnavailableRpc,
    VmSignInRpc,
)
from igni_core.backend.rpc_binding import Correlation, NoArgs, RpcBinding, rpc
from igni_core.backend.rpc_streams import RUN_EVENTS, TOOL_PUSHES
from igni_core.backend.schemas_extensions import PageTokenReply
from igni_core.backend.schemas_pool import AttachRefused
from igni_core.backend.schemas_rpc import AttachSessionResult
from igni_core.extensions.host.window_errors import WindowModelError, WindowStateError
from igni_core.extensions.host.window_rpc import Hydrated, Written
from igni_core.extensions.host.wire import Reply
from igni_core.extensions.service_events import DeliveryReceipt, NotHandled
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.rpc import RpcErrorCode


class _Transport:
    def __init__(self) -> None:
        self.sent: list[msg.Message] = []

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)

    def receive(self) -> Any:
        raise AssertionError("not read here")


def _router() -> tuple[PoolRpcRouter, _Transport]:
    transport = _Transport()
    return PoolRpcRouter(PoolReplier(transport)), transport  # type: ignore[arg-type]


def _call(
    method: RpcMethod,
    args: dict[str, Any] | None = None,
    *,
    caller: str | None = None,
    client: str = "",
) -> msg.RPCRequest:
    request = msg.RPCRequest(
        id=f"req-{method.value}", method=method.value, args=args or {}, caller=caller
    )
    if client:
        request.mark_from(client)
    return request


async def _answer(
    router: PoolRpcRouter, transport: _Transport, request: msg.RPCRequest
) -> msg.RPCResponse:
    await router.handle(request)
    responses = [m for m in transport.sent if isinstance(m, msg.RPCResponse)]
    assert len(responses) == 1
    assert responses[0].id == request.id
    return responses[0]


# ── router ────────────────────────────────────────────────────────


async def test_an_unmounted_router_says_why_for_each_call_it_cannot_answer() -> None:
    expected = {
        RpcMethod.HOST_MODEL: HostModelRpcs.PAGE_ONLY,
        RpcMethod.WINDOW_MODEL: HostModelRpcs.NO_HOST_MODEL,
        RpcMethod.EXTENSION_SERVICE_TOKEN: PageTokenRpc.PAGE_ONLY,
        RpcMethod.VM_SIGN_IN: VmSignInRpc.NO_SESSION,
        RpcMethod.DELIVER_SERVICE_EVENTS: ServiceEventRpc.NO_INBOX,
    }
    for method, reason in expected.items():
        router, transport = _router()
        request = _call(method, caller="x")
        assert router.handles(request)
        response = await _answer(router, transport, request)
        assert (response.error, response.code) == (reason, RpcErrorCode.UNAVAILABLE), method


async def test_an_unmounted_router_mints_no_approver_token_and_revokes_none() -> None:
    router, transport = _router()
    minted = await _answer(router, transport, _call(RpcMethod.MINT_APPROVER_TOKEN))
    assert minted.code == RpcErrorCode.UNAVAILABLE
    assert "takes no token" in (minted.error or "")
    assert minted.error.startswith("mint_approver_token:")  # type: ignore[union-attr]

    router, transport = _router()
    revoked = await _answer(router, transport, _call(RpcMethod.REVOKE_APPROVER_TOKENS))
    assert revoked.error is None
    assert revoked.result == {"revoked": 0}


def test_the_router_handles_only_a_request_for_a_method_in_its_table() -> None:
    router, _ = _router()
    assert router.handles(_call(RpcMethod.VM_SIGN_IN))
    assert not router.handles(msg.RPCRequest(method="no_such_method"))
    assert not router.handles(msg.RPCRequest(method=RpcMethod.ATTACH_SESSION.value))
    assert not router.handles(msg.Info(text="hello"))


async def test_mounting_a_handler_replaces_the_one_that_answered_before() -> None:
    router, transport = _router()

    class _Ready(BaseModel):
        state: str = "ready"
        message: str = ""

    async def keeper(token: str, organisation: str | None, /) -> BaseModel:
        return _Ready()

    router.mount(VmSignInRpc(keeper))
    first = await _answer(router, transport, _call(RpcMethod.VM_SIGN_IN, {"session_token": "t"}))
    assert first.result == {"state": "ready", "message": ""}

    async def other(token: str, organisation: str | None, /) -> BaseModel:
        return _Ready(state="needs_sign_in", message="again")

    router.mount(VmSignInRpc(other))
    transport.sent.clear()
    second = await _answer(router, transport, _call(RpcMethod.VM_SIGN_IN, {"session_token": "t"}))
    assert second.result == {"state": "needs_sign_in", "message": "again"}


def test_the_router_exposes_the_replier_it_answers_on() -> None:
    router, _ = _router()
    assert isinstance(router.replier, PoolReplier)


def test_the_pool_rpcs_are_the_six_handlers_and_publish_every_method_they_answer() -> None:
    assert set(POOL_RPCS) == {
        AttachSessionRpc,
        HostModelRpcs,
        PageTokenRpc,
        VmSignInRpc,
        ServiceEventRpc,
        ApproverTokenRpc,
    }
    methods: set[RpcMethod] = set()
    for handler in POOL_RPCS:
        assert handler.methods == frozenset(RpcBinding.on(handler))
        methods |= handler.methods
    assert methods == {
        RpcMethod.ATTACH_SESSION,
        RpcMethod.HOST_MODEL,
        RpcMethod.WINDOW_MODEL,
        RpcMethod.EXTENSION_SERVICE_TOKEN,
        RpcMethod.VM_SIGN_IN,
        RpcMethod.DELIVER_SERVICE_EVENTS,
        RpcMethod.MINT_APPROVER_TOKEN,
        RpcMethod.REVOKE_APPROVER_TOKENS,
    }


# ── base ──────────────────────────────────────────────────────────


class _Out(BaseModel):
    when: Any = None


class _Probe(PoolRpc):
    JSON_DUMPED = frozenset({RpcMethod.VM_SIGN_IN})

    def __init__(self, behaviour: Any) -> None:
        self._behaviour = behaviour

    @rpc(RpcMethod.ATTACH_SESSION, args=NoArgs, scope=RpcScope.IGNI)
    async def python_dumped(self, args: NoArgs, request: msg.RPCRequest) -> Any:
        return await self._run()

    @rpc(RpcMethod.VM_SIGN_IN, args=NoArgs, scope=RpcScope.IGNI)
    def json_dumped(self, args: NoArgs, request: msg.RPCRequest) -> Any:
        return self._behaviour()

    async def _run(self) -> Any:
        return self._behaviour()


async def _probe(behaviour: Any, method: RpcMethod, args: dict[str, Any] | None = None) -> Any:
    transport = _Transport()
    replier = PoolReplier(transport)  # type: ignore[arg-type]
    request = _call(method, args)
    await _Probe(behaviour).handle(request, replier)
    return transport.sent[0], request


async def test_a_call_that_breaks_is_answered_with_its_error_and_code() -> None:
    def boom() -> Any:
        raise OSError("disk full")

    for method in (RpcMethod.ATTACH_SESSION, RpcMethod.VM_SIGN_IN):  # async and sync handler
        response, request = await _probe(boom, method)
        assert response.id == request.id
        assert response.error == f"{method.value}: disk full"
        assert response.code == RpcErrorCode.FAILED


async def test_a_call_that_raises_a_value_error_is_refused_not_failed() -> None:
    def refuse() -> Any:
        raise ValueError("no")

    response, _ = await _probe(refuse, RpcMethod.VM_SIGN_IN)
    assert (response.error, response.code) == ("vm_sign_in: no", RpcErrorCode.REFUSED)


async def test_a_refusal_is_answered_with_its_reason_and_its_own_code() -> None:
    def default() -> Any:
        raise PoolRefusal("not now")

    def custom() -> Any:
        raise PoolRefusal("who are you", RpcErrorCode.FORBIDDEN)

    response, _ = await _probe(default, RpcMethod.VM_SIGN_IN)
    assert (response.error, response.code) == ("not now", RpcErrorCode.REFUSED)
    response, _ = await _probe(custom, RpcMethod.ATTACH_SESSION)
    assert (response.error, response.code) == ("who are you", RpcErrorCode.FORBIDDEN)


async def test_a_call_whose_arguments_do_not_fit_is_answered_invalid() -> None:
    response, _ = await _probe(lambda: _Out(), RpcMethod.VM_SIGN_IN, {"stray": 1})
    assert response.code == RpcErrorCode.INVALID
    assert response.error


async def test_a_handler_that_answers_something_that_is_no_model_is_a_type_error() -> None:
    transport = _Transport()
    request = _call(RpcMethod.VM_SIGN_IN)
    with pytest.raises(TypeError, match="vm_sign_in answered dict, not a model"):
        await _Probe(lambda: {"a": 1}).handle(request, PoolReplier(transport))  # type: ignore[arg-type]
    assert transport.sent == []


async def test_a_result_is_dumped_as_json_only_for_the_calls_that_always_were() -> None:
    from datetime import datetime

    stamp = datetime(2026, 1, 2, 3, 4, 5)
    json_response, _ = await _probe(lambda: _Out(when=stamp), RpcMethod.VM_SIGN_IN)
    python_response, _ = await _probe(lambda: _Out(when=stamp), RpcMethod.ATTACH_SESSION)
    assert json_response.error is None and python_response.error is None
    assert json_response.result == {"when": "2026-01-02T03:04:05"}
    assert python_response.result == {"when": stamp}


async def test_an_error_that_is_logged_names_the_method(caplog: pytest.LogCaptureFixture) -> None:
    def boom() -> Any:
        raise RuntimeError("kaput")

    with caplog.at_level(logging.ERROR):
        await _probe(boom, RpcMethod.VM_SIGN_IN)
    assert any("vm_sign_in failed: kaput" in r.getMessage() for r in caplog.records)


def test_a_pool_rpc_without_tagged_methods_has_no_methods_of_its_own() -> None:
    class _Bare(PoolRpc):
        pass

    assert "methods" not in vars(_Bare)


async def test_an_unavailable_slot_ignores_its_arguments_and_says_why() -> None:
    slot = UnavailableRpc(RpcMethod.VM_SIGN_IN, "nothing here")
    assert slot.methods == frozenset({RpcMethod.VM_SIGN_IN})
    answer = await slot.answer(_call(RpcMethod.VM_SIGN_IN, {"anything": [1, 2]}))
    assert (answer.error, answer.code, answer.result) == (
        "nothing here",
        RpcErrorCode.UNAVAILABLE,
        None,
    )


# ── attach_session ────────────────────────────────────────────────


class _Runtime:
    def __init__(self, fail: bool = False) -> None:
        self.replayed: list[str] = []
        self._fail = fail

    async def replay_waiting_approvals(self, request_id: str = "") -> None:
        if self._fail:
            raise RuntimeError("replay broke")
        self.replayed.append(request_id)


class _Pool:
    def __init__(self, runtime: _Runtime) -> None:
        self.runtime = runtime
        self.asked: list[str] = []

    async def get_or_create(self, session_id: str) -> _Runtime:
        self.asked.append(session_id)
        return self.runtime


class _Sessions:
    def __init__(self, outcome: AttachSessionResult | AttachRefused, runtime: _Runtime) -> None:
        self.outcome = outcome
        self.pool = _Pool(runtime)
        self.attached: list[tuple[str, str | None]] = []

    async def attach(self, session_id: str, project_dir: str | None) -> Any:
        self.attached.append((session_id, project_dir))
        return self.outcome


def _attach_router(sessions: _Sessions) -> tuple[PoolRpcRouter, _Transport]:
    router, transport = _router()
    router.mount(AttachSessionRpc(sessions))  # type: ignore[arg-type]
    return router, transport


async def test_an_attach_is_answered_then_waiting_approvals_are_shown_with_its_id() -> None:
    runtime = _Runtime()
    sessions = _Sessions(AttachSessionResult(session_id="s1", project_dir="/p"), runtime)
    router, transport = _attach_router(sessions)
    request = _call(
        RpcMethod.ATTACH_SESSION, {"session_id": "s1", "project_dir": "/p"}, client="w1"
    )

    response = await _answer(router, transport, request)

    assert response.result == {"session_id": "s1", "project_dir": "/p"}
    assert response.to_client == "w1"
    assert sessions.attached == [("s1", "/p")]
    assert sessions.pool.asked == ["s1"]
    assert request.id == "req-attach_session"
    assert runtime.replayed == ["req-attach_session"]


async def test_an_attach_with_nothing_asks_for_the_default_session() -> None:
    runtime = _Runtime()
    sessions = _Sessions(AttachSessionResult(session_id="d", project_dir="/home"), runtime)
    router, transport = _attach_router(sessions)

    await _answer(router, transport, _call(RpcMethod.ATTACH_SESSION))

    assert sessions.attached == [("", "")]


async def test_an_attach_that_is_refused_is_refused_and_shows_nothing_again() -> None:
    runtime = _Runtime()
    sessions = _Sessions(AttachRefused(reason="s1 belongs to /other"), runtime)
    router, transport = _attach_router(sessions)

    response = await _answer(
        router, transport, _call(RpcMethod.ATTACH_SESSION, {"session_id": "s1"})
    )

    assert (response.error, response.code) == ("s1 belongs to /other", RpcErrorCode.REFUSED)
    assert sessions.pool.asked == []
    assert runtime.replayed == []


async def test_a_replay_that_fails_after_an_attach_is_logged_and_the_answer_stands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = _Sessions(
        AttachSessionResult(session_id="s1", project_dir="/p"), _Runtime(fail=True)
    )
    router, transport = _attach_router(sessions)

    with caplog.at_level(logging.ERROR):
        response = await _answer(router, transport, _call(RpcMethod.ATTACH_SESSION))

    assert response.error is None
    assert response.result == {"session_id": "s1", "project_dir": "/p"}
    assert [m for m in transport.sent if not isinstance(m, msg.RPCResponse)] == []
    assert any("replaying approvals after attach failed" in r.getMessage() for r in caplog.records)


async def test_an_attach_that_breaks_is_answered_failed_not_left_to_time_out() -> None:
    class _Broken(_Sessions):
        async def attach(self, session_id: str, project_dir: str | None) -> Any:
            raise OSError("db gone")

    sessions = _Broken(AttachRefused(reason=""), _Runtime())
    router, transport = _attach_router(sessions)

    response = await _answer(router, transport, _call(RpcMethod.ATTACH_SESSION))

    assert (response.error, response.code) == ("attach_session: db gone", RpcErrorCode.FAILED)


def test_attach_session_is_published_as_a_correlated_io_stream_of_run_events() -> None:
    binding = RpcBinding.on(AttachSessionRpc)[RpcMethod.ATTACH_SESSION][1]
    assert binding.correlate == Correlation(by="id", source="request.id")
    assert binding.duration == RpcDuration.IO
    assert binding.scope == RpcScope.SESSION
    assert binding.streams == RUN_EVENTS
    assert binding.may_stream == TOOL_PUSHES


# ── window_model and host_model ───────────────────────────────────


class _WindowRpc:
    def __init__(self) -> None:
        self.seen: list[Any] = []
        self.raises: Exception | None = None

    def handle(self, request: Any) -> Any:
        self.seen.append(request)
        if self.raises is not None:
            raise self.raises
        if request.op == "hydrate":
            return Hydrated(state={}, rev=3, unset=[])  # type: ignore[arg-type]
        return Written(rev=4)


class _Registry:
    def __init__(self, windows: dict[str, str]) -> None:
        self._windows = windows

    def by_connection(self, connection_id: str, /) -> str | None:
        return self._windows.get(connection_id)


class _Model:
    def __init__(self, windows: dict[str, str] | None = None) -> None:
        self.window_rpc = _WindowRpc()
        self.registry = _Registry(windows or {})
        self.pages: list[tuple[str, dict[str, Any]]] = []
        self.reply: Reply = Reply.answered("page", "fine")

    @property
    def services(self) -> Any:
        return self

    @property
    def windows(self) -> Any:
        return self

    async def handle_page(self, extension_id: str, args: dict[str, Any], /) -> Reply:
        self.pages.append((extension_id, args))
        return self.reply


def _model_router(model: _Model) -> tuple[PoolRpcRouter, _Transport]:
    router, transport = _router()
    router.mount(HostModelRpcs(model))  # type: ignore[arg-type]
    return router, transport


async def test_a_window_reads_its_model_and_the_connection_it_is_on_wins() -> None:
    model = _Model()
    router, transport = _model_router(model)
    request = _call(
        RpcMethod.WINDOW_MODEL,
        {"op": "hydrate", "window": "w", "connection_id": "from-args"},
        client="conn-7",
    )

    response = await _answer(router, transport, request)

    assert response.error is None
    assert response.result["rev"] == 3  # type: ignore[index]
    assert model.window_rpc.seen[0].connection_id == "conn-7"
    assert model.window_rpc.seen[0].window == "w"


async def test_a_window_on_a_transport_without_connections_names_itself() -> None:
    model = _Model()
    router, transport = _model_router(model)
    request = _call(
        RpcMethod.WINDOW_MODEL, {"op": "hydrate", "window": "w", "connection_id": "from-args"}
    )

    await _answer(router, transport, request)

    assert model.window_rpc.seen[0].connection_id == "from-args"


async def test_a_write_the_model_refuses_is_refused_with_the_models_reason() -> None:
    model = _Model()
    model.window_rpc.raises = WindowModelError("no such field")
    router, transport = _model_router(model)

    response = await _answer(
        router,
        transport,
        _call(RpcMethod.WINDOW_MODEL, {"op": "write", "window": "w", "field": "x", "value": 1}),
    )

    assert (response.error, response.code) == (
        "window_model: no such field",
        RpcErrorCode.REFUSED,
    )


async def test_a_window_whose_saved_state_cannot_be_written_is_told_it_failed() -> None:
    model = _Model()
    model.window_rpc.raises = WindowStateError("disk full")
    router, transport = _model_router(model)

    response = await _answer(
        router, transport, _call(RpcMethod.WINDOW_MODEL, {"op": "hydrate", "window": "w"})
    )

    assert (response.error, response.code) == ("window_model: disk full", RpcErrorCode.FAILED)


@pytest.mark.parametrize(
    "args",
    [{"op": "bogus", "window": "w"}, {"op": "hydrate", "window": ""}, {"window": "w"}],
)
async def test_a_window_request_that_does_not_fit_is_invalid(args: dict[str, Any]) -> None:
    router, transport = _model_router(_Model())

    response = await _answer(router, transport, _call(RpcMethod.WINDOW_MODEL, args))

    assert response.code == RpcErrorCode.INVALID


async def test_a_page_request_goes_to_the_host_model_as_its_extension() -> None:
    model = _Model()
    router, transport = _model_router(model)

    response = await _answer(
        router,
        transport,
        _call(RpcMethod.HOST_MODEL, {"op": "get", "path": "host.chat", "id": 9}, caller="ext.a"),
    )

    assert response.error is None
    assert response.result == {
        "op": "reply",
        "id": "page",
        "ok": True,
        "result": "fine",
        "error": None,
    }
    extension, wire = model.pages[0]
    assert extension == "ext.a"
    assert wire["op"] == "get" and wire["id"] == 9
    # only what the page set is forwarded: no defaulted params, value or home_window
    assert "home_window" not in wire
    assert "params" not in wire and "value" not in wire


async def test_the_window_framing_a_page_replaces_the_home_window_it_sent() -> None:
    model = _Model({"conn-1": "window-A"})
    router, transport = _model_router(model)

    await _answer(
        router,
        transport,
        _call(
            RpcMethod.HOST_MODEL,
            {"op": "get", "path": "p", "home_window": "page-said"},
            caller="ext.a",
            client="conn-1",
        ),
    )
    assert model.pages[0][1]["home_window"] == "window-A"

    # a connection that frames no window leaves the page's own home_window
    model.pages.clear()
    transport.sent.clear()
    await _answer(
        router,
        transport,
        _call(
            RpcMethod.HOST_MODEL,
            {"op": "get", "path": "p", "home_window": "page-said"},
            caller="ext.a",
            client="conn-unknown",
        ),
    )
    assert model.pages[0][1]["home_window"] == "page-said"


async def test_a_page_request_without_a_caller_is_forbidden() -> None:
    model = _Model()
    router, transport = _model_router(model)

    response = await _answer(router, transport, _call(RpcMethod.HOST_MODEL, {"op": "get"}))

    assert (response.error, response.code) == (HostModelRpcs.PAGE_ONLY, RpcErrorCode.FORBIDDEN)
    assert model.pages == []


async def test_a_malformed_page_request_is_answered_with_a_refused_reply_not_an_error() -> None:
    model = _Model()
    router, transport = _model_router(model)

    response = await _answer(
        router,
        transport,
        _call(RpcMethod.HOST_MODEL, {"id": 12, "op": "no-such-op"}, caller="ext.a"),
    )

    assert response.error is None and response.code is None
    reply = Reply.model_validate(response.result)
    assert reply.ok is False
    assert reply.id == "12"
    assert reply.error is not None
    assert reply.error.code.value == "invalid"
    assert reply.error.message.startswith("not a host-model request: ")
    assert model.pages == []


async def test_a_malformed_page_request_without_an_id_is_answered_as_page() -> None:
    router, transport = _model_router(_Model())

    response = await _answer(
        router, transport, _call(RpcMethod.HOST_MODEL, {"op": "no-such-op"}, caller="ext.a")
    )

    assert Reply.model_validate(response.result).id == "page"


def test_the_host_calls_are_published_with_their_correlation_duration_and_streams() -> None:
    bound = RpcBinding.on(HostModelRpcs)
    window = bound[RpcMethod.WINDOW_MODEL][1]
    assert window.correlate == Correlation(by="payload.token", source="args.token")
    assert window.streams == (PushChannel.WINDOW_MODEL_CHANGED,)
    page = bound[RpcMethod.HOST_MODEL][1]
    assert page.correlate == Correlation(by="payload.extension_id", source="request.caller")
    assert page.duration == RpcDuration.PERSON
    assert PushChannel.HOST_SIGNAL in page.streams
    assert page.may_stream == TOOL_PUSHES


def test_the_window_model_arguments_describe_every_field_for_outside_developers() -> None:
    from igni_core.backend.pool_rpcs import host_model

    fields = host_model.WindowModelArgs.model_fields
    assert set(fields) == {
        "op",
        "window",
        "field",
        "value",
        "token",
        "dialog_id",
        "kind",
        "connection_id",
    }
    assert all(f.description for f in fields.values())


# ── extension_service_token ───────────────────────────────────────


class _Tokens:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.raises: Exception | None = None

    async def reply_for(self, extension_id: str) -> PageTokenReply:
        self.asked.append(extension_id)
        if self.raises:
            raise self.raises
        return PageTokenReply(
            ok=True, access_token=f"tok-{extension_id}", expires_in=60, service_url="https://svc"
        )


async def test_a_page_gets_a_token_for_the_extension_recorded_as_its_caller() -> None:
    tokens = _Tokens()
    router, transport = _router()
    router.mount(PageTokenRpc(tokens))  # type: ignore[arg-type]

    one = await _answer(
        router,
        transport,
        _call(RpcMethod.EXTENSION_SERVICE_TOKEN, caller="ext.one", client="w"),
    )
    transport.sent.clear()
    two = await _answer(
        router, transport, _call(RpcMethod.EXTENSION_SERVICE_TOKEN, caller="ext.two")
    )

    assert one.result["access_token"] == "tok-ext.one"  # type: ignore[index]
    assert two.result["access_token"] == "tok-ext.two"  # type: ignore[index]
    assert tokens.asked == ["ext.one", "ext.two"]


@pytest.mark.parametrize("caller", [None, ""])
async def test_a_service_token_without_a_caller_is_forbidden(caller: str | None) -> None:
    tokens = _Tokens()
    router, transport = _router()
    router.mount(PageTokenRpc(tokens))  # type: ignore[arg-type]

    response = await _answer(
        router, transport, _call(RpcMethod.EXTENSION_SERVICE_TOKEN, caller=caller)
    )

    assert (response.error, response.code) == (PageTokenRpc.PAGE_ONLY, RpcErrorCode.FORBIDDEN)
    assert tokens.asked == []


async def test_a_service_token_call_with_stray_arguments_is_invalid() -> None:
    router, transport = _router()
    router.mount(PageTokenRpc(_Tokens()))  # type: ignore[arg-type]

    response = await _answer(
        router,
        transport,
        _call(RpcMethod.EXTENSION_SERVICE_TOKEN, {"audience": "other"}, caller="ext.one"),
    )

    assert response.code == RpcErrorCode.INVALID


async def test_an_exchange_that_times_out_is_answered_not_left_waiting() -> None:
    tokens = _Tokens()
    tokens.raises = TimeoutError("exchange timed out")
    router, transport = _router()
    router.mount(PageTokenRpc(tokens))  # type: ignore[arg-type]

    response = await _answer(
        router, transport, _call(RpcMethod.EXTENSION_SERVICE_TOKEN, caller="ext.one")
    )

    assert response.error == "extension_service_token: exchange timed out"
    assert response.code == RpcErrorCode.FAILED


def test_the_service_token_call_is_published_as_a_network_call() -> None:
    binding = RpcBinding.on(PageTokenRpc)[RpcMethod.EXTENSION_SERVICE_TOKEN][1]
    assert binding.duration == RpcDuration.NETWORK
    assert binding.scope == RpcScope.IGNI


# ── vm_sign_in and deliver_service_events ─────────────────────────


class _State(BaseModel):
    state: str
    message: str = ""


class _Keeper:
    def __init__(self, state: str = "ready") -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.state = state
        self.raises: Exception | None = None

    async def __call__(self, session_token: str, organisation: str | None, /) -> BaseModel:
        self.calls.append((session_token, organisation))
        if self.raises:
            raise self.raises
        return _State(state=self.state, message="m")


async def test_a_vm_signs_in_and_the_keepers_state_is_the_answer() -> None:
    keeper = _Keeper("needs_reissue")
    router, transport = _router()
    router.mount(VmSignInRpc(keeper))

    response = await _answer(
        router,
        transport,
        _call(RpcMethod.VM_SIGN_IN, {"session_token": "tok", "organisation": "acme"}),
    )

    assert response.result == {"state": "needs_reissue", "message": "m"}
    assert keeper.calls == [("tok", "acme")]


async def test_a_sign_in_without_an_organisation_signs_in_for_the_persons_own() -> None:
    keeper = _Keeper()
    router, transport = _router()
    router.mount(VmSignInRpc(keeper))

    await _answer(router, transport, _call(RpcMethod.VM_SIGN_IN, {"session_token": "tok"}))
    transport.sent.clear()
    await _answer(
        router,
        transport,
        _call(RpcMethod.VM_SIGN_IN, {"session_token": "tok", "organisation": None}),
    )

    assert keeper.calls == [("tok", None), ("tok", None)]


@pytest.mark.parametrize("args", [{}, {"session_token": ""}, {"session_token": 7}])
async def test_a_sign_in_without_a_session_token_is_invalid_and_asks_nothing(
    args: dict[str, Any],
) -> None:
    keeper = _Keeper()
    router, transport = _router()
    router.mount(VmSignInRpc(keeper))

    response = await _answer(router, transport, _call(RpcMethod.VM_SIGN_IN, args))

    assert (response.error, response.code) == (VmSignInRpc.NO_SESSION, RpcErrorCode.INVALID)
    assert keeper.calls == []


async def test_a_keeper_that_fails_is_answered_with_its_error() -> None:
    keeper = _Keeper()
    keeper.raises = OSError("disk full")
    router, transport = _router()
    router.mount(VmSignInRpc(keeper))

    response = await _answer(
        router, transport, _call(RpcMethod.VM_SIGN_IN, {"session_token": "tok"})
    )

    assert (response.error, response.code) == ("vm_sign_in: disk full", RpcErrorCode.FAILED)


async def test_a_keeper_answering_a_state_nobody_published_is_answered_failed() -> None:
    router, transport = _router()
    router.mount(VmSignInRpc(_Keeper("exploded")))

    response = await _answer(
        router, transport, _call(RpcMethod.VM_SIGN_IN, {"session_token": "tok"})
    )

    assert response.result is None
    assert response.error and response.error.startswith("vm_sign_in:")


class _Inbox:
    def __init__(self, receipt: DeliveryReceipt) -> None:
        self.receipt = receipt
        self.batches: list[list[Any]] = []

    async def deliver(self, events: list[Any]) -> DeliveryReceipt:
        self.batches.append(events)
        return self.receipt


async def test_a_batch_of_events_is_delivered_and_each_outcome_is_answered() -> None:
    inbox = _Inbox(
        DeliveryReceipt(
            delivered=["e1"],
            duplicate=["e2"],
            failed=["e3"],
            not_handled=[NotHandled(event_id="e4", reason="no handler")],
        )
    )
    router, transport = _router()
    router.mount(ServiceEventRpc(inbox))  # type: ignore[arg-type]
    events = [{"event_id": f"e{i}", "extension_id": "ext"} for i in range(1, 5)]

    response = await _answer(
        router, transport, _call(RpcMethod.DELIVER_SERVICE_EVENTS, {"events": events})
    )

    assert [e.event_id for e in inbox.batches[0]] == ["e1", "e2", "e3", "e4"]
    assert response.result["delivered"] == ["e1"]  # type: ignore[index]
    assert response.result["duplicate"] == ["e2"]  # type: ignore[index]
    assert response.result["failed"] == ["e3"]  # type: ignore[index]
    assert response.result["not_handled"] == [{"event_id": "e4", "reason": "no handler"}]  # type: ignore[index]
    # the failed one is left unacknowledged so it is handed out again
    assert response.result["acknowledge"] == ["e1", "e4", "e2"]  # type: ignore[index]


async def test_an_empty_batch_is_delivered_and_acknowledges_nothing() -> None:
    inbox = _Inbox(DeliveryReceipt())
    router, transport = _router()
    router.mount(ServiceEventRpc(inbox))  # type: ignore[arg-type]

    response = await _answer(
        router, transport, _call(RpcMethod.DELIVER_SERVICE_EVENTS, {"events": []})
    )

    assert inbox.batches == [[]]
    assert response.result["acknowledge"] == []  # type: ignore[index]


@pytest.mark.parametrize("args", [{"events": "nope"}, {"events": [{"extension_id": "x"}]}])
async def test_something_that_is_not_a_batch_of_events_is_refused_as_not_events(
    args: dict[str, Any],
) -> None:
    inbox = _Inbox(DeliveryReceipt())
    router, transport = _router()
    router.mount(ServiceEventRpc(inbox))  # type: ignore[arg-type]

    response = await _answer(router, transport, _call(RpcMethod.DELIVER_SERVICE_EVENTS, args))

    assert response.code == RpcErrorCode.INVALID
    assert response.error and response.error.startswith("not events: ")
    assert inbox.batches == []


def test_the_platform_calls_are_published_with_their_channels_durations_and_streams() -> None:
    sign_in = RpcBinding.on(VmSignInRpc)[RpcMethod.VM_SIGN_IN][1]
    assert sign_in.correlate == Correlation(by="channel")
    assert sign_in.duration == RpcDuration.NETWORK
    assert sign_in.streams == (PushChannel.VM_CREDENTIAL,)
    deliver = RpcBinding.on(ServiceEventRpc)[RpcMethod.DELIVER_SERVICE_EVENTS][1]
    assert deliver.correlate == Correlation(by="channel")
    assert deliver.duration == RpcDuration.IO
    assert msg.UserMessageReceived in deliver.streams
    assert set(RUN_EVENTS) <= set(deliver.streams)
    assert deliver.may_stream == TOOL_PUSHES


# ── approver tokens ───────────────────────────────────────────────


class _Minter:
    def __init__(self, tokens: list[str | None]) -> None:
        self._tokens = tokens
        self.minted: list[str] = []

    def mint_approver_token(self) -> str | None:
        token = self._tokens.pop(0)
        if token is not None:
            self.minted.append(token)
        return token

    async def revoke_approver_tokens(self) -> int:
        count, self.minted = len(self.minted), []
        return count


async def test_a_minted_token_is_the_answer_and_a_revoke_counts_what_it_forgot() -> None:
    minter = _Minter(["a", "b"])
    router, transport = _router()
    router.mount(ApproverTokenRpc(minter))

    first = await _answer(router, transport, _call(RpcMethod.MINT_APPROVER_TOKEN, client="w"))
    transport.sent.clear()
    second = await _answer(router, transport, _call(RpcMethod.MINT_APPROVER_TOKEN, client="w"))
    transport.sent.clear()
    revoked = await _answer(router, transport, _call(RpcMethod.REVOKE_APPROVER_TOKENS))
    transport.sent.clear()
    again = await _answer(router, transport, _call(RpcMethod.REVOKE_APPROVER_TOKENS))

    assert (first.result, second.result) == ({"token": "a"}, {"token": "b"})
    assert revoked.result == {"revoked": 2}
    assert again.result == {"revoked": 0}


async def test_a_transport_that_takes_no_token_mints_none() -> None:
    router, transport = _router()
    router.mount(ApproverTokenRpc(_Minter([None])))

    response = await _answer(router, transport, _call(RpcMethod.MINT_APPROVER_TOKEN))

    assert response.code == RpcErrorCode.UNAVAILABLE
    assert "this igni takes no token" in (response.error or "")


async def test_an_approver_call_with_stray_arguments_is_invalid() -> None:
    router, transport = _router()

    response = await _answer(
        router, transport, _call(RpcMethod.MINT_APPROVER_TOKEN, {"scope": "all"})
    )

    assert response.code == RpcErrorCode.INVALID
