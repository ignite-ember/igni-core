"""The account-only backend signs the person in, and runs nothing.

``docs/design/local-cloud-switch.md`` §6.4. A desktop whose group runs
igni in the cloud starts ``python -m igni_core.backend --account-only``.
It serves signing in and out, finding and choosing the server, the plan
and the status — with the same arguments and answers as a full backend —
and keeps the group pack fresh. It builds no session: no agent, tools,
MCP server, extension, watcher or event pull. A chat message gets the run
gate's refusal; every other call is ``unavailable``.

The calls are driven through :class:`AccountDispatcher` with the real
controllers, and the whole process through :meth:`AccountBackend.run` on a
loopback WebSocket, as the shell starts it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from websockets.asyncio.client import connect

from igni_core.backend import __main__ as backend_main
from igni_core.backend.account_backend import (
    ACCOUNT_RPCS,
    AccountBackend,
    AccountControllers,
    AccountDispatcher,
    AccountHolder,
    AccountRpcs,
    AccountServer,
    AccountStatus,
)
from igni_core.backend.group_policy_sync import GroupPolicySync
from igni_core.backend.lockfile import Lockfile
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.rpc_binding import RpcBinding
from igni_core.backend.rpc_handlers.auth import AuthRpcHandler
from igni_core.backend.rpc_handlers.session import SessionRpcHandler
from igni_core.backend.schemas_lifecycle import BackendLaunch
from igni_core.backend.schemas_rpc import LifecyclePhase, LoginResult
from igni_core.backend.server_auth import AuthController
from igni_core.core.auth.discovery import ServerDiscovery
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.config.group_policy.gate import GatePhase, RunGate
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.meta import PackMeta, PackMetaStore
from igni_core.core.config.group_policy.pack import PackTheme
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.config.user_config_store import UserConfigStore
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.enums import CommandResultKind, PushChannel
from igni_core.protocol.schemas.envelope import Standing
from igni_core.protocol.schemas.rpc import RpcErrorCode
from tests.group_policy_signing import ISSUER, PERSON, sign_in
from tests.host_model_helpers import CapturingTransport

REASON = "Your organisation runs igni for you in the cloud."
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the loop's signal handlers")


def _home() -> Path:
    return Path.home() / ".igni"


def _configure(*, signed_in: bool = True) -> None:
    _home().mkdir(parents=True, exist_ok=True)
    (_home() / "config.yaml").write_text(f"api_url: {ISSUER}\n")
    if signed_in:
        sign_in(_home() / "credentials.json")


class Rig:
    """The account backend's parts, over a transport that keeps what is sent."""

    def __init__(self) -> None:
        settings = SettingsLoader.load()
        self.settings = settings
        self.holder = AccountHolder(settings)
        self.portal = PortalClient(api_url=settings.api_url, portal_url=None)
        self.sync = GroupPolicySync(self.holder, settings, self.portal)
        self.meta = PackMetaStore(GroupPolicyLayout.for_data_dir(settings.storage.data_dir))
        self.status = AccountStatus(self.holder, self.meta)
        self.auth = AuthController(
            self.holder,
            settings,
            self.status,
            portal=self.portal,
            group_policy=self.sync,
            user_config_store=UserConfigStore(),
            server_discovery=ServerDiscovery(),
        )
        self.windows = CapturingTransport()
        push = PushNotificationBridge(transport=self.windows, loop=None, queue=[])
        self.login = LoginCoordinator(
            backend=AccountServer(AccountControllers(auth=self.auth)), push_bridge=push
        )
        self.gate = RunGate()
        self.gate.close(REASON)
        self.dispatcher = AccountDispatcher(
            table=AccountRpcs(auth=self.auth, login=self.login, status=self.status).table(),
            login=self.login,
            gate=self.gate,
            transport=self.windows,
        )

    async def call(self, method: str, args: dict[str, Any] | None = None, **fields: Any) -> Any:
        request = msg.RPCRequest(id=f"r-{method}", method=method, args=args or {}, **fields)
        request.mark_from("window-1")
        await self.dispatcher.dispatch(request)
        answers = [m for m in self.windows.sent if m.id == request.id]
        assert len(answers) == 1, answers
        assert answers[0].to_client == "window-1", "answered to the caller alone"
        return answers[0]


@pytest.fixture
def rig() -> Rig:
    _configure()
    return Rig()


# ── What it answers ───────────────────────────────────────────────


def test_it_answers_sign_in_the_server_the_plan_and_the_status():
    assert {
        RpcMethod.LOGIN,
        RpcMethod.RELOAD_CLOUD_CREDENTIALS,
        RpcMethod.CLEAR_CLOUD_CREDENTIALS,
        RpcMethod.GET_CLOUD_PLAN,
        RpcMethod.DISCOVER_SERVER,
        RpcMethod.SET_SERVER,
        RpcMethod.GET_STATUS,
    } == ACCOUNT_RPCS


def test_its_calls_are_the_full_backend_s_calls():
    """The same methods, bound by the same declarations: one contract."""
    declared = {**RpcBinding.on(AuthRpcHandler), **RpcBinding.on(SessionRpcHandler)}
    assert declared.keys() >= ACCOUNT_RPCS


async def test_the_status_says_who_is_signed_in_as_a_status_update(rig: Rig):
    answer = await rig.call("get_status")

    assert isinstance(answer, msg.StatusUpdate), "sent in place of an rpc_response"
    assert answer.cloud_email == f"{PERSON}@example.invalid"
    assert answer.cloud_connected is True
    assert answer.theme is None


async def test_the_status_carries_the_group_s_theme(rig: Rig):
    rig.meta.save(PackMeta(group_id="g1", theme=PackTheme.model_validate({"accent": "#123456"})))

    answer = await rig.call("get_status")

    assert answer.theme == {"accent": "#123456"}


async def test_nobody_signed_in_is_not_connected_and_has_no_plan():
    _configure(signed_in=False)
    rig = Rig()

    status = await rig.call("get_status")
    plan = await rig.call("get_cloud_plan")

    assert status.cloud_connected is False and status.cloud_email == ""
    assert isinstance(plan, msg.RPCResponse) and plan.error is None and plan.result is None


async def test_a_sign_in_made_elsewhere_is_read_again(rig: Rig):
    sign_in(_home() / "credentials.json", sub="person-2")

    answer = await rig.call("reload_cloud_credentials")

    assert isinstance(answer, msg.StatusUpdate)
    assert answer.cloud_email == "person-2@example.invalid"


async def test_signing_out_forgets_the_saved_sign_in(rig: Rig):
    answer = await rig.call("clear_cloud_credentials")

    assert isinstance(answer, msg.StatusUpdate)
    assert answer.cloud_connected is False
    assert not (_home() / "credentials.json").exists()


async def test_discovery_answers_without_a_session(rig: Rig):
    answer = await rig.call("discover_server", {"email": "not an address"})

    assert answer.error is None
    assert answer.result["api_url"] == ""
    assert answer.result["reason"] == "That does not look like an email address."


async def test_choosing_a_server_writes_it_down_and_points_the_sign_in_there(rig: Rig):
    answer = await rig.call("set_server", {"api_url": "https://igni.acme.example"})

    assert answer.error is None
    assert answer.result["api_url"] == "https://igni.acme.example"
    assert "igni.acme.example" in (_home() / "config.yaml").read_text()
    assert rig.holder.cloud_auth.server_url == "https://igni.acme.example"


async def test_a_server_that_is_not_https_is_refused(rig: Rig):
    answer = await rig.call("set_server", {"api_url": "http://igni.acme.example"})

    assert answer.result["error"] == "http://igni.acme.example is not an https address."


@pytest.mark.parametrize(
    ("method", "args", "said"),
    [
        ("discover_server", {}, "discover_server: 'email' is required"),
        ("get_status", {"verbose": True}, "get_status: 'verbose' is not an argument of this call"),
    ],
)
async def test_arguments_that_do_not_fit_are_refused_as_a_full_backend_refuses_them(
    rig: Rig, method: str, args: dict[str, Any], said: str
):
    answer = await rig.call(method, args)

    assert answer.code is RpcErrorCode.INVALID
    assert answer.error == said


async def test_a_call_that_fails_is_answered_with_the_failure(rig: Rig, monkeypatch):
    async def broken(email: str) -> Any:
        raise RuntimeError("the resolver is gone")

    monkeypatch.setattr(rig.auth, "discover_server", broken)

    answer = await rig.call("discover_server", {"email": "sam@acme.example"})

    assert answer.code is RpcErrorCode.FAILED
    assert answer.error == "the resolver is gone"


async def test_signing_in_starts_a_flow_and_ends_it_with_one_result(rig: Rig, monkeypatch):
    async def signed_in(on_status: Any = None) -> LoginResult:
        await on_status("Waiting for login in browser...")
        return LoginResult(ok=True, email="sam@acme.example")

    monkeypatch.setattr(rig.auth, "login", signed_in)

    answer = await rig.call("login")

    assert answer.error is None and answer.result["started"] is True
    flow = answer.result["flow_id"]
    async with asyncio.timeout(5):
        while not rig.windows.pushes(PushChannel.LOGIN_RESULT):
            await asyncio.sleep(0.01)
    (result,) = rig.windows.pushes(PushChannel.LOGIN_RESULT)
    assert result.payload["success"] is True and result.payload["flow_id"] == flow
    (status,) = rig.windows.pushes(PushChannel.LOGIN_STATUS)
    assert status.payload["text"] == "Waiting for login in browser..."


async def test_a_cancelled_sign_in_ends_with_a_cancelled_result(rig: Rig, monkeypatch):
    async def never(on_status: Any = None) -> LoginResult:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr(rig.auth, "login", never)
    await rig.call("login")

    await rig.dispatcher.dispatch(msg.CancelLogin())

    (result,) = rig.windows.pushes(PushChannel.LOGIN_RESULT)
    assert result.payload == {
        "success": False,
        "result": "cancelled",
        "flow_id": result.payload["flow_id"],
    }


# ── What it refuses ───────────────────────────────────────────────


async def test_a_chat_message_gets_the_run_gate_s_refusal_and_its_stream_ends(rig: Rig):
    message = msg.UserMessage(id="u1", text="hello")
    message.mark_from("window-1")

    await rig.dispatcher.dispatch(message)

    error, end = rig.windows.sent
    assert isinstance(error, msg.Error) and error.text == REASON
    assert isinstance(end, msg.StreamEnd) and end.id == "u1"


async def test_a_slash_command_is_refused_with_the_same_words(rig: Rig):
    command = msg.Command(id="c1", text="/model")

    await rig.dispatcher.dispatch(command)

    (result,) = rig.windows.sent
    assert isinstance(result, msg.CommandResult)
    assert result.kind is CommandResultKind.ERROR and result.content == REASON


async def test_without_a_reason_the_gate_s_own_words_are_said(rig: Rig):
    rig.dispatcher._gate = RunGate()

    await rig.dispatcher.dispatch(msg.Cancel(id="x"))

    assert rig.windows.of(msg.Error)[0].text == REASON


_EVERYTHING_ELSE = sorted(set(RpcMethod) - ACCOUNT_RPCS)


@pytest.mark.parametrize("method", _EVERYTHING_ELSE)
async def test_every_other_call_is_unavailable(rig: Rig, method: RpcMethod):
    answer = await rig.call(method.value)

    assert isinstance(answer, msg.RPCResponse)
    assert answer.code is RpcErrorCode.UNAVAILABLE
    assert answer.error == f"{method.value}: this igni only signs you in ({REASON})"


async def test_an_extension_s_namespace_is_unavailable(rig: Rig):
    answer = await rig.call("ext.acme.notes.search")

    assert answer.code is RpcErrorCode.UNAVAILABLE


async def test_a_call_speaking_for_an_extension_is_unavailable_even_an_account_call(rig: Rig):
    answer = await rig.call("get_status", caller="acme.notes")

    assert answer.code is RpcErrorCode.UNAVAILABLE


async def test_a_program_s_standing_is_judged_first(rig: Rig):
    request = msg.RPCRequest(id="s", method="get_status", args={}, caller="acme.notes")
    request.mark_from("program-1", Standing.PROGRAM)

    await rig.dispatcher.dispatch(request)

    (answer,) = rig.windows.sent
    assert answer.code is RpcErrorCode.FORBIDDEN
    assert answer.error == "get_status: only igni's window speaks for an extension"


# ── The process ───────────────────────────────────────────────────


@contextlib.contextmanager
def _signal_handlers_restored():
    loop = asyncio.get_running_loop()
    try:
        yield
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGUSR1", None)):
            if sig is not None:
                loop.remove_signal_handler(sig)


async def _until(predicate, timeout: float = 30) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition never held")
        await asyncio.sleep(0.02)


class Nothing:
    """What must never start in the account-only backend: each records a call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started: list[str] = []
        from igni_core.backend import app, extensions_runtime, session_runtime_factory
        from igni_core.core.mcp import client
        from igni_core.extensions import event_pull
        from igni_core.extensions.distribution import sync as distribution

        def refuse(name: str):
            def started(*args: Any, **kwargs: Any) -> Any:
                self.started.append(name)
                raise AssertionError(f"{name} started in the account-only backend")

            return started

        monkeypatch.setattr(app.BackendApp, "run", refuse("the full backend"))
        monkeypatch.setattr(extensions_runtime.ExtensionsRuntime, "build", refuse("the extensions"))
        monkeypatch.setattr(
            session_runtime_factory.SessionRuntimeFactory, "boot", refuse("a session")
        )
        monkeypatch.setattr(client.MCPClientManager, "__init__", refuse("an MCP manager"))
        monkeypatch.setattr(event_pull.PersonEventPull, "start", refuse("the event pull"))
        # Nor the distributed extensions' sync, at start or on its timer.
        monkeypatch.setattr(
            distribution.ExtensionDistribution, "__init__", refuse("the extension distribution")
        )
        monkeypatch.setattr(
            distribution.ExtensionDistribution,
            "start_polling",
            refuse("the extension distribution's timer"),
        )


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".igni").mkdir()
    monkeypatch.delenv("IGNI_PARENT_PID", raising=False)
    return tmp_path


@POSIX_ONLY
async def test_the_process_serves_its_window_and_runs_nothing(project, capsys, monkeypatch):
    _configure()
    nothing = Nothing(monkeypatch)
    hydrated: list[str] = []

    async def hydrate(self: GroupPolicySync, token: str, *, force: bool = False) -> bool:
        hydrated.append(token)
        return False

    monkeypatch.setattr(GroupPolicySync, "hydrate", hydrate)
    app = AccountBackend(BackendLaunch(ws_port=0, project_dir=project))
    assert app.serving_port is None
    with _signal_handlers_restored():
        running = asyncio.create_task(app.run())
        try:
            await _until(lambda: app.serving_port is not None or running.done())
            assert not running.done(), running.exception()
            port = app.serving_port
            ready = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
            assert ready == {"status": "ready", "ws_port": port, "ws_url": f"ws://127.0.0.1:{port}"}
            assert Lockfile(project).read() is None, "nothing that looks for a backend finds it"
            assert app.gate.phase is GatePhase.closed and app.gate.reason == REASON

            async with connect(f"ws://127.0.0.1:{port}") as window:
                welcome = json.loads(await asyncio.wait_for(window.recv(), 10))
                assert welcome["type"] == "welcome"

                await window.send(msg.RPCRequest(id="s", method="get_status").model_dump_json())
                status = json.loads(await asyncio.wait_for(window.recv(), 10))
                assert status["type"] == "status_update" and status["id"] == "s"
                assert status["cloud_email"] == f"{PERSON}@example.invalid"

                await window.send(msg.UserMessage(id="u", text="hi").model_dump_json())
                refused = json.loads(await asyncio.wait_for(window.recv(), 10))
                assert refused["type"] == "error" and refused["text"] == REASON

                await _until(lambda: bool(hydrated))
                await window.send(msg.Shutdown().model_dump_json())
                await asyncio.wait_for(running, 30)
        finally:
            if not running.done():
                app.request_shutdown()
                await asyncio.wait_for(running, 30)

    assert nothing.started == []
    assert hydrated, "the pack sync ran with the stored sign-in"
    assert app.supervisor is not None and app.supervisor.phase is LifecyclePhase.STOPPED
    assert app.serving_port is None


@POSIX_ONLY
async def test_a_signal_stops_it_before_any_window_came(project, capsys):
    _configure(signed_in=False)
    app = AccountBackend(BackendLaunch(ws_port=0, project_dir=project))
    with _signal_handlers_restored():
        running = asyncio.create_task(app.run())
        await _until(lambda: app.serving_port is not None or running.done())
        assert not running.done(), running.exception()

        app.request_shutdown()
        await asyncio.wait_for(running, 30)

    assert app.supervisor is not None and app.supervisor.phase is LifecyclePhase.STOPPED


@POSIX_ONLY
async def test_a_boot_that_fails_still_tears_down(project, monkeypatch, caplog):
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(
        "igni_core.backend.account_backend.BackendSettingsLoader.load", broken, raising=True
    )
    app = AccountBackend(BackendLaunch(ws_port=0, project_dir=project))
    with _signal_handlers_restored():
        await asyncio.wait_for(app.run(), 30)

    assert "settings exploded" in caplog.text
    assert app.supervisor is not None and app.supervisor.phase is LifecyclePhase.STOPPED


def test_the_flag_starts_the_account_only_backend_and_not_the_full_one(project, monkeypatch):
    started: list[BackendLaunch] = []

    async def run(self: AccountBackend) -> None:
        started.append(self._launch)

    monkeypatch.setattr(AccountBackend, "run", run)
    Nothing(monkeypatch)

    result = CliRunner().invoke(
        backend_main.main, ["--ws-port", "0", "--account-only", "--project-dir", str(project)]
    )

    assert result.exit_code == 0, result.output
    assert len(started) == 1 and started[0].ws_port == 0


def test_without_the_flag_the_full_backend_starts(project, monkeypatch):
    from igni_core.backend import app

    started: list[str] = []

    async def run(self: Any) -> None:
        started.append("full")

    monkeypatch.setattr(app.BackendApp, "run", run)

    result = CliRunner().invoke(
        backend_main.main, ["--ws-port", "0", "--project-dir", str(project)]
    )

    assert result.exit_code == 0, result.output
    assert started == ["full"]


def test_the_help_says_what_the_flag_serves():
    result = CliRunner().invoke(backend_main.main, ["--help"])

    assert "--account-only" in result.output
    assert "no agent" in result.output
