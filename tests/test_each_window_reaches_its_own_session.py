"""Each window's messages reach its own session, and pool-wide calls are answered once.

:class:`SessionOrchestrator` is the "one backend, many live sessions"
switchboard: every message a window sends passes through
:meth:`dispatch`, which either answers it at the pool level (attach a
session, the host and window models, a page's service token, the
platform's events and VM sign-in) or routes it by its ``session_id`` to
that session's runtime, resuming the session when it is not live. Its
tests so far reached single handlers through ``__new__``; the routing,
the attach rules and the pool's set-up had none.

These build the orchestrator through :meth:`SessionOrchestrator.start`
with the real :class:`SessionPool`, :class:`SessionDirectoryStore` (SQLite
under ``tmp_path``), :class:`RpcRouter`, :class:`PushNotificationBridge`
and :class:`MessageDispatcher`. The seam is the session's backend: a
:class:`BackendServer` boots a model team, so a small recording backend
stands in for it — the boot one passed in, and pooled ones made where
:meth:`SessionRuntimeFactory.create` makes them.

Pinned:

* a window attaching with a project directory gets a session there, and
  the same session again on the next attach;
* a session whose directory has gone resumes in the backend's own;
* a session from another project is refused, and so is a directory that
  is not one;
* attaching shows the session's waiting approvals again;
* messages route by ``session_id``; the session list is always the boot
  project's; a crash in routing is an ``Error`` for that window;
* the window and host models, the platform's events and the VM sign-in
  answer when they can and say why when they cannot;
* sessions idle past ``IGNI_SESSION_IDLE_TIMEOUT`` are let go, and a
  value that is not a number is ignored;
* ``serve`` stops at ``Shutdown`` and lets in-flight messages finish.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.pool_rpcs import HostModelRpcs, ServiceEventRpc, VmSignInRpc
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.rpc_handlers.pool_guard import PLATFORM_RPCS, POOL_LEVEL_RPCS
from igni_core.backend.rpc_router import RpcRouter
from igni_core.backend.schemas_pool import AttachRefused, ExtensionPost, ProjectDirs
from igni_core.backend.schemas_sessions import AutoNameResult
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.backend.session_runtime_factory import BootRuntime, SessionRuntimeFactory
from igni_core.core.broadcast_schema import BroadcastSink
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.settings import Settings
from igni_core.core.layer import layers
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator
from igni_core.extensions.host.window_errors import WindowModelError
from igni_core.extensions.service_events import DeliveryReceipt
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import manifest


class _Hitl:
    def __init__(self, waiting: list[Any]) -> None:
        self._waiting = waiting

    def waiting_messages(self) -> list[Any]:
        return list(self._waiting)

    async def resume_interrupted(self) -> AsyncIterator[Any]:
        for m in self._waiting:
            yield msg.Info(text=f"resuming {m.run_id}")


class _Backend:
    """What the orchestrator asks of a session's backend."""

    def __init__(self, session_id: str, project_dir: Path, waiting: list[Any] | None = None):
        self.session_id = session_id
        self.project_dir = project_dir
        # The supervisor the backend process shares — the RPC router
        # reads it to build the shell runner.
        self.supervisor = ProcessSupervisor()
        # Whether this backend process may run agent sessions. The real
        # ``BackendServer`` carries one; the RPC router reads it to build
        # the workflow runner. This one is open, so a run starts.
        self.gate = RunGate()
        self.processing = False
        self.runtime: Any = None
        self.message_queue: list[str] = []
        self.controllers = SimpleNamespace(hitl=_Hitl(waiting or []))
        self.reloads = 0
        self.broadcasts: list[Any] = []
        self.handled: list[str] = []
        self.shut_down = False

    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime

    def wire_queue_hook(self, queue: Any) -> None:
        self.queue_hook = queue

    def register_broadcast(self, subscriber: BroadcastSink) -> None:
        self.broadcasts.append(subscriber)

    def reload_agents(self) -> None:
        self.reloads += 1

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        self.shut_down = True

    def consume_plan_research_flag(self) -> bool:
        return False

    async def run_message(self, text: str, media: dict[str, str]) -> AsyncIterator[Any]:
        self.handled.append(text)
        yield msg.Info(text="answered")

    async def maybe_auto_name_session(self) -> AutoNameResult:
        return AutoNameResult(ok=False, reason="already_named")

    async def list_sessions(self) -> Any:
        self.handled.append("list_sessions")
        return msg.Info(text=f"sessions of {self.session_id}")

    async def handle_command(self, text: str) -> Any:
        self.handled.append(text)
        if text == "/crash":
            raise RuntimeError("command exploded")
        return msg.Info(text=f"{self.session_id} ran {text}")


class _Pooled(_Backend):
    """Stands in for ``BackendServer`` where the pool resumes a session."""

    made: list[_Pooled] = []

    def __init__(
        self,
        settings: Settings,
        *,
        project_dir: Path,
        resume_session_id: str,
        additional_dirs: Any,
        contributions: Any = None,
        prompt_loader: Any = None,
        supervisors: SupervisorLocator | None = None,
        gate: RunGate | None = None,
    ) -> None:
        super().__init__(resume_session_id, project_dir)
        # The resumed session's processes land on the supervisor the
        # backend process shares — the one the pool wiring carries.
        if supervisors is not None:
            self.supervisor = supervisors.supervisor
        # So do its runs ask the gate of the backend process.
        if gate is not None:
            self.gate = gate
        _Pooled.made.append(self)


class _Transport:
    def __init__(self, incoming: list[Any] | None = None) -> None:
        self.sent: list[Any] = []
        self._incoming = incoming or []

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    async def receive(self) -> AsyncIterator[Any]:
        for m in self._incoming:
            yield m


class _WindowAnswer(BaseModel):
    window: str
    op: str


class _PageAnswer(BaseModel):
    caller: str
    args: dict[str, Any]


class _WindowRpc:
    def handle(self, request: Any) -> _WindowAnswer:
        if request.field == "nope":
            raise WindowModelError("no field 'nope'")
        return _WindowAnswer(window=request.window, op=request.op)


class _Windows:
    @property
    def registry(self) -> _Windows:
        return self

    def by_connection(self, connection_id: str) -> str | None:
        return None


class _HostServices:
    def __init__(self) -> None:
        self.window_rpc = _WindowRpc()
        self.windows = _Windows()


class _HostModel:
    def __init__(self) -> None:
        self.services = _HostServices()

    async def handle_page(self, caller: str, args: dict) -> _PageAnswer:
        return _PageAnswer(caller=caller, args=args)


class _Inbox:
    def __init__(self) -> None:
        self.got: list[Any] = []

    async def deliver(self, events: list[Any]) -> DeliveryReceipt:
        self.got.extend(events)
        return DeliveryReceipt(delivered=[e.event_id for e in events])


class _Credential(BaseModel):
    """The VM's credential state, as a layer's credential keeper answers it."""

    state: str
    message: str = ""


class _PooledRuntimes(SessionRuntimeFactory):
    server_cls = _Pooled  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _pooled_backends():
    _Pooled.made = []


def _orchestrator(
    tmp_path: Path, *, waiting: list[Any] | None = None, incoming: list[Any] | None = None
) -> tuple[SessionOrchestrator, _Backend, _Transport]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    settings = Settings(storage={"data_dir": str(tmp_path / "data")})
    backend = _Backend("boot", project, waiting)
    transport = _Transport(incoming)
    queue: list[str] = []
    push = PushNotificationBridge(transport=transport, loop=None, queue=queue)
    login = LoginCoordinator(backend=backend, push_bridge=push)
    router = RpcRouter(backend=backend, login=login, push=push)
    orchestrator = SessionOrchestrator.start(
        boot=BootRuntime(
            backend=backend, rpc_table=router.build_table(), router=router, queue=queue
        ),
        wiring=PoolWiring(
            transport=transport,
            push_bridge=push,
            login=login,
            supervisors=SupervisorLocator(backend.supervisor),
            gate=backend.gate,
        ),
        settings=settings,
        dirs=ProjectDirs(project_dir=project),
        factory=_PooledRuntimes,
    )
    return orchestrator, backend, transport


def _rpc(method: str, request_id: str = "q", caller: str | None = None, **args: Any) -> Any:
    return msg.RPCRequest(id=request_id, method=method, args=args, caller=caller)


# ── Set-up ────────────────────────────────────────────────────────


async def test_the_boot_session_is_the_pool_s_default(tmp_path):
    orchestrator, backend, _ = _orchestrator(tmp_path)

    assert orchestrator.pool.default.backend is backend
    assert backend.runtime is orchestrator.pool.default
    assert orchestrator.client_state is not None
    assert orchestrator.extensions is not None


async def test_refreshing_agents_rebuilds_every_live_session(tmp_path):
    orchestrator, backend, _ = _orchestrator(tmp_path)
    await orchestrator.attach("other", None)

    orchestrator.refresh_agents()

    assert backend.reloads == 1
    assert [b.reloads for b in _Pooled.made] == [1]


# ── Attaching a window ────────────────────────────────────────────


async def test_a_window_attaching_with_a_directory_gets_a_session_there(tmp_path):
    orchestrator, _, _ = _orchestrator(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    first = await orchestrator.attach("s-9", str(elsewhere))
    # A reload passes its folder again (``chat/sessionBinding.ts``).
    again = await orchestrator.attach("s-9", str(elsewhere))

    assert (first.session_id, first.project_dir) == ("s-9", str(elsewhere.resolve()))
    assert again == first
    assert len(_Pooled.made) == 1
    assert _Pooled.made[0].runtime is not None


async def test_a_new_window_with_a_directory_and_no_session_gets_a_new_one(tmp_path):
    orchestrator, _, _ = _orchestrator(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = await orchestrator.attach("", str(elsewhere))

    assert result.session_id and result.session_id != "boot"
    assert result.project_dir == str(elsewhere.resolve())


async def test_a_window_with_neither_gets_a_fresh_session_in_the_backend_s_project(tmp_path):
    orchestrator, backend, _ = _orchestrator(tmp_path)

    result = await orchestrator.attach("", None)

    assert result.session_id not in ("", "boot")
    assert result.project_dir == str(backend.project_dir)


async def test_a_session_whose_directory_is_gone_resumes_in_the_backend_s_own(tmp_path):
    orchestrator, backend, _ = _orchestrator(tmp_path)
    gone = tmp_path / "gone"
    gone.mkdir()
    await orchestrator.attach("old", str(gone))
    # A second backend process sees the registry, not the live runtime.
    later, _, _ = _orchestrator(tmp_path)
    gone.rmdir()

    refused = await later.attach("old", None)
    assert isinstance(refused, AttachRefused)
    assert "different project" in refused.reason
    result = await later.pool.get_or_create("old")

    assert result.backend.project_dir == backend.project_dir


async def test_a_directory_that_is_not_one_is_refused(tmp_path):
    orchestrator, _, transport = _orchestrator(tmp_path)

    await orchestrator.dispatch(
        _rpc(RpcMethod.ATTACH_SESSION, "a", project_dir=str(tmp_path / "missing"))
    )

    (reply,) = transport.sent
    assert reply.id == "a"
    assert reply.error.startswith("not a directory:")


async def test_attaching_shows_the_session_s_waiting_approvals_again(tmp_path):
    waiting = [msg.RunPaused(run_id="r-1")]
    orchestrator, _, transport = _orchestrator(tmp_path, waiting=waiting)

    await orchestrator.dispatch(_rpc(RpcMethod.ATTACH_SESSION, "a", session_id="boot"))

    reply, paused, resumed, end = transport.sent
    assert reply.result == {"session_id": "boot", "project_dir": str(tmp_path / "project")}
    assert paused.run_id == "r-1"
    assert resumed.text == "resuming r-1"
    # Through the session's stamped transport: the window knows whose it is.
    assert paused.session_id == "boot"
    # Each carries the attach's id, and a stream_end of it ends them: the
    # client that attached can tell what its attach brought back.
    assert [m.id for m in (paused, resumed, end)] == ["a", "a", "a"]
    assert isinstance(end, msg.StreamEnd)


# ── Routing ───────────────────────────────────────────────────────


async def test_a_message_goes_to_the_session_it_is_stamped_with(tmp_path):
    orchestrator, backend, transport = _orchestrator(tmp_path)
    seen: list[Any] = []
    orchestrator.watch_inbound(seen.append)
    await orchestrator.attach("s-2", None)
    command = msg.Command(id="c", text="/help", session_id="s-2")

    await orchestrator.dispatch(command)
    await orchestrator.dispatch(msg.Command(id="d", text="/help"))

    (pooled,) = _Pooled.made
    assert pooled.handled == ["/help"]
    assert backend.handled == ["/help"]
    assert [m.text for m in transport.sent] == ["s-2 ran /help", "boot ran /help"]
    assert [m.session_id for m in transport.sent] == ["s-2", "boot"]
    assert seen[0] is command


async def test_the_session_list_is_always_the_boot_project_s(tmp_path):
    orchestrator, backend, transport = _orchestrator(tmp_path)
    await orchestrator.attach("s-2", None)

    await orchestrator.dispatch(msg.SessionList(id="l", session_id="s-2"))

    assert backend.handled == ["list_sessions"]
    assert transport.sent[0].text == "sessions of boot"


async def test_a_crash_while_routing_is_an_error_for_that_window(tmp_path):
    orchestrator, _, transport = _orchestrator(tmp_path)

    await orchestrator.dispatch(msg.Command(id="x", text="/crash", session_id="boot"))

    (error,) = transport.sent
    assert error.type == "error"
    assert (error.id, error.session_id) == ("x", "boot")
    assert error.text == "session routing failed: command exploded"


async def test_an_extension_s_message_reaches_its_session_now_or_queued(tmp_path):
    orchestrator, _, transport = _orchestrator(tmp_path)
    await orchestrator.attach("s-3", None)
    (pooled,) = _Pooled.made

    await orchestrator.post_from_extension(
        ExtensionPost(session_id="s-3", extension_id="acme", text="later", media=None, queued=True)
    )

    # The session's own queue — the next turn of *that* session reads it.
    assert pooled.message_queue[-1].endswith("later\n</extension-message>")
    assert 'from="acme"' in pooled.message_queue[-1]
    assert transport.sent[-1].queued is True


async def test_an_extension_s_message_now_runs_a_turn_in_its_session(tmp_path):
    orchestrator, _, transport = _orchestrator(tmp_path)
    await orchestrator.attach("s-4", None)
    (pooled,) = _Pooled.made

    await orchestrator.post_from_extension(
        ExtensionPost(
            session_id="s-4", extension_id="acme", text="ticket closed", media=None, queued=False
        )
    )
    for _ in range(100):
        if transport.sent and transport.sent[-1].type == "stream_end":
            break
        await asyncio.sleep(0.01)

    assert [m.type for m in transport.sent] == ["user_message_received", "info", "stream_end"]
    assert transport.sent[0].extension_id == "acme"
    assert {m.session_id for m in transport.sent} == {"s-4"}
    assert 'from="acme"' in pooled.handled[0]


async def test_a_spawned_dispatch_runs_in_the_background(tmp_path):
    orchestrator, backend, transport = _orchestrator(tmp_path)

    task = orchestrator.spawn_dispatch(msg.Command(id="c", text="/help"))
    await task

    assert backend.handled == ["/help"]
    assert transport.sent[0].id == "c"


# ── Pool-level calls ──────────────────────────────────────────────


async def test_the_window_model_answers_and_refuses_with_the_reason(tmp_path):
    orchestrator, _, transport = _orchestrator(tmp_path)

    await orchestrator.dispatch(_rpc(RpcMethod.WINDOW_MODEL, "0", op="hydrate", window="w"))
    orchestrator.mount(HostModelRpcs(_HostModel()))
    await orchestrator.dispatch(_rpc(RpcMethod.WINDOW_MODEL, "1", op="hydrate", window="w"))
    await orchestrator.dispatch(
        _rpc(RpcMethod.WINDOW_MODEL, "2", op="write", window="w", field="nope")
    )
    await orchestrator.dispatch(_rpc(RpcMethod.WINDOW_MODEL, "3", op="fly", window="w"))

    none, ok, refused, invalid = transport.sent
    assert none.error == "window_model: no host model"
    assert ok.result == {"window": "w", "op": "hydrate"}
    assert refused.error == "window_model: no field 'nope'"
    assert invalid.error.startswith("window_model: ")


async def test_the_host_model_answers_only_an_extension_s_page(tmp_path):
    orchestrator, _, transport = _orchestrator(tmp_path)
    orchestrator.mount(HostModelRpcs(_HostModel()))
    orchestrator.extensions.registry.install(manifest("acme.reviewer"))

    await orchestrator.dispatch(_rpc(RpcMethod.HOST_MODEL, "1", path="host.sessions"))
    await orchestrator.dispatch(
        _rpc(RpcMethod.HOST_MODEL, "2", caller="acme.reviewer", op="get", path="x")
    )

    refused, answered = transport.sent
    assert refused.error == "host_model: only an extension's page reaches the host model"
    assert answered.result == {"caller": "acme.reviewer", "args": {"op": "get", "path": "x"}}


async def test_nothing_speaks_for_an_extension_that_is_not_installed(tmp_path):
    # `caller` is a field of the message; an id nothing installed answers to
    # is refused before any handler hears it.
    orchestrator, _, transport = _orchestrator(tmp_path)
    host_model = _HostModel()
    orchestrator.mount(HostModelRpcs(host_model))

    await orchestrator.dispatch(_rpc(RpcMethod.HOST_MODEL, "2", caller="nobody", path="x"))

    assert "no extension 'nobody' is installed" in transport.sent[0].error


async def test_a_page_s_token_needs_a_token_source(tmp_path):
    orchestrator, _, transport = _orchestrator(tmp_path)
    orchestrator.extensions.registry.install(manifest("acme.reviewer"))

    await orchestrator.dispatch(
        _rpc(RpcMethod.EXTENSION_SERVICE_TOKEN, "t", caller="acme.reviewer")
    )

    assert "only an extension's page asks for one" in transport.sent[0].error


async def test_the_platform_s_events_are_delivered_and_acknowledged(tmp_path):
    orchestrator, _, transport = _orchestrator(tmp_path)
    event = {"event_id": "e1", "extension_id": "acme", "type": "index.ready"}

    await orchestrator.dispatch(_rpc(RpcMethod.DELIVER_SERVICE_EVENTS, "0", events=[event]))
    inbox = _Inbox()
    orchestrator.mount(ServiceEventRpc(inbox))
    await orchestrator.dispatch(_rpc(RpcMethod.DELIVER_SERVICE_EVENTS, "1", events=[event]))
    await orchestrator.dispatch(_rpc(RpcMethod.DELIVER_SERVICE_EVENTS, "2", events=[{"x": 1}]))

    none, delivered, malformed = transport.sent
    assert none.error == "this igni takes no events"
    assert delivered.result["acknowledge"] == ["e1"]
    assert [e.event_id for e in inbox.got] == ["e1"]
    assert malformed.error.startswith("not events:")


async def test_the_vm_signs_in_with_the_person_s_session(tmp_path):
    orchestrator, _, transport = _orchestrator(tmp_path)
    calls: list[tuple[str, str | None]] = []

    async def sign_in(token: str, organisation: str | None) -> _Credential:
        calls.append((token, organisation))
        return _Credential(state="ready", message=organisation or "")

    await orchestrator.dispatch(_rpc(RpcMethod.VM_SIGN_IN, "0", session_token="tok"))
    orchestrator.mount(VmSignInRpc(sign_in))
    await orchestrator.dispatch(_rpc(RpcMethod.VM_SIGN_IN, "1", session_token=""))
    await orchestrator.dispatch(
        _rpc(RpcMethod.VM_SIGN_IN, "2", session_token="tok", organisation="acme")
    )
    await orchestrator.dispatch(
        _rpc(RpcMethod.VM_SIGN_IN, "3", session_token="tok", organisation=7)
    )

    no_keeper, no_token, with_org, bad_org = transport.sent
    assert "a producer's VM takes the person's session_token" in no_keeper.error
    assert no_token.error == no_keeper.error
    assert with_org.result == {"state": "ready", "message": "acme"}
    assert bad_org.result == {"state": "ready", "message": ""}
    assert calls == [("tok", "acme"), ("tok", None)]


class _ControlOnly:
    def platform_calls_need_control(self) -> bool:
        return True


async def test_a_layer_can_take_platform_calls_from_its_control_connection_only(
    tmp_path, monkeypatch
):
    orchestrator, _, transport = _orchestrator(tmp_path)
    orchestrator.mount(ServiceEventRpc(_Inbox()))
    monkeypatch.setattr(layers, "current", lambda: _ControlOnly())

    await orchestrator.dispatch(_rpc(RpcMethod.DELIVER_SERVICE_EVENTS, "w", events=[]))
    control = _rpc(RpcMethod.DELIVER_SERVICE_EVENTS, "c", events=[])
    control.mark_control()
    await orchestrator.dispatch(control)

    from_window, from_control = transport.sent
    assert from_window.error == "deliver_service_events: only on the platform's control connection"
    assert from_control.result["acknowledge"] == []


def test_the_platform_s_calls_are_checked_before_any_pool_call_is_answered():
    # Admission refuses a platform call from the wrong connection before the
    # pool's table is consulted; that is the order it always had only while
    # none of these is a platform call.
    assert PLATFORM_RPCS <= POOL_LEVEL_RPCS
    assert not PLATFORM_RPCS & {
        RpcMethod.ATTACH_SESSION,
        RpcMethod.HOST_MODEL,
        RpcMethod.EXTENSION_SERVICE_TOKEN,
    }


# ── Lifetime ──────────────────────────────────────────────────────


@pytest.mark.parametrize("value,evicted", [("0.01", True), ("soon", False)])
async def test_sessions_idle_past_the_timeout_are_let_go(tmp_path, monkeypatch, value, evicted):
    monkeypatch.setenv("IGNI_SESSION_IDLE_TIMEOUT", value)
    orchestrator, _, _ = _orchestrator(tmp_path)
    await orchestrator.attach("idle", None)
    await asyncio.sleep(0.05)

    report = await orchestrator.pool.evict_idle()

    assert bool(report.evicted) is evicted
    assert _Pooled.made[0].shut_down is evicted


async def test_serve_stops_at_shutdown_and_lets_messages_finish(tmp_path):
    incoming = [
        msg.Command(id="1", text="/help"),
        msg.Shutdown(),
        msg.Command(id="never", text="/help"),
    ]
    orchestrator, backend, transport = _orchestrator(tmp_path, incoming=incoming)

    await orchestrator.serve(asyncio.Event())

    assert backend.handled == ["/help"]
    assert [m.id for m in transport.sent] == ["1"]


async def test_serve_stops_once_shutdown_is_set(tmp_path):
    orchestrator, backend, _ = _orchestrator(tmp_path, incoming=[msg.Command(id="1", text="/x")])
    stop = asyncio.Event()
    stop.set()

    await orchestrator.serve(stop)

    assert backend.handled == []


async def test_a_pool_level_call_that_breaks_is_answered_with_its_error(tmp_path):
    """A pool-level call that raised used to reach the crash boundary: no
    answer for the caller, who waited out its timeout, and "session routing
    failed" for every window. It is answered, to the caller, with the error."""
    orchestrator, _, transport = _orchestrator(tmp_path)

    async def sign_in(token: str, organisation: str | None) -> _Credential:
        raise OSError("disk full")

    orchestrator.mount(VmSignInRpc(sign_in))
    await orchestrator.dispatch(_rpc(RpcMethod.VM_SIGN_IN, "b1", session_token="tok"))

    (answer,) = transport.sent
    assert isinstance(answer, msg.RPCResponse)
    assert answer.id == "b1"
    assert answer.error == "vm_sign_in: disk full"
