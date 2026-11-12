"""A headless run keeps its extensions' state, refuses what needs a window, and bounds their start.

An extension in an ``igni -m`` run reaches the host model like any
other, but no window stands behind it: what would act on one is
refused with a reason, what would list one is empty, and what a window
would remember is a scratchpad that lives as long as the run. Starting
the installed extensions is bounded, and one that did not come up in
time is ``FAILED`` — so the hooks it holds shut stay shut.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.activity import ActivityState
from igni_core.backend.headless_extensions import (
    HeadlessExtensions,
    HeadlessHostBackend,
    RunState,
)
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_gate import extension_hook_gate
from igni_core.core.hooks.extension_values import PreToolUseValue
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.core.prompts import PromptFragments, PromptLoader
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.launcher import ExtensionLauncher
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionState
from igni_core.extensions.store import ExtensionStore
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod

# ── What a window would remember ──────────────────────────────────


def test_a_write_that_worked_says_ok_and_reads_back():
    state = RunState()

    written = state.set_value("panel", "theme", "dark")
    read = state.get_for_client("panel")

    assert written.ok and written.error == ""
    assert read.ok and read.values == {"theme": "dark"}


def test_a_delete_removes_the_key_and_says_ok_for_one_that_was_never_there():
    state = RunState()
    state.set_value("panel", "a", "1")
    state.set_value("panel", "b", "2")

    removed = state.delete_value("panel", "a")
    absent = state.delete_value("panel", "never-set")

    assert removed.ok and absent.ok
    assert state.get_for_client("panel").values == {"b": "2"}


def test_one_client_never_sees_another_s_keys():
    state = RunState()
    state.set_value("one", "k", "mine")
    state.set_value("two", "k", "yours")

    assert state.get_for_client("one").values == {"k": "mine"}
    assert state.get_for_client("two").values == {"k": "yours"}
    stranger = state.get_for_client("three")
    assert stranger.ok and stranger.values == {}


def test_an_empty_or_unicode_client_and_key_are_kept_like_any_other():
    state = RunState()
    state.set_value("", "", "empty")
    state.set_value("клиент", "ключ", "значення")

    assert state.get_for_client("").values == {"": "empty"}
    assert state.get_for_client("клиент").values == {"ключ": "значення"}


def test_a_later_write_replaces_the_earlier_one():
    state = RunState()
    state.set_value("c", "k", "first")
    state.set_value("c", "k", "second")

    assert state.get_for_client("c").values == {"k": "second"}


# ── What needs a window is refused, with the reason ───────────────


def _backend(tmp_path: Path) -> HeadlessHostBackend:
    return HeadlessHostBackend(tmp_path, "s-1")


async def _refusal(call: Any) -> HostModelError:
    with pytest.raises(HostModelError) as caught:
        await call
    return caught.value


async def test_every_member_that_would_act_on_a_window_is_refused_and_names_what_it_was(
    tmp_path: Path,
):
    backend = _backend(tmp_path)
    media = ChatAttachments()
    refused = {
        "rpc": await _refusal(backend.rpc(RpcMethod.LIST_SESSIONS, {}, session_id="s-1")),
        "act": await _refusal(backend.act(msg.Info(text="x"), session_id="s-1")),
        "ask": await _refusal(backend.ask(msg.Info(text="x"), session_id="s-1")),
        "attach": await _refusal(backend.attach("s-1", None)),
        "post": await _refusal(
            backend.post(session_id="s-1", extension_id="e", text="t", media=media, queued=False)
        ),
        "deliver": await _refusal(
            backend.deliver(session_id="s-1", extension_id="e", text="t", media=media)
        ),
        "title": await _refusal(backend.title(session_id="s-1", title="t")),
        "show_view": await _refusal(
            backend.show_view(session_id="s-1", payload=None, run_id="r")  # type: ignore[arg-type]
        ),
    }

    assert {name: e.code for name, e in refused.items()} == dict.fromkeys(
        refused, ErrorCode.REFUSED
    )
    suffix = ": this is a headless run, with no window"
    assert refused["rpc"].message == RpcMethod.LIST_SESSIONS.value + suffix
    assert refused["act"].message == "Info" + suffix
    assert refused["ask"].message == "Info" + suffix
    assert refused["attach"].message == "attach_session" + suffix
    assert refused["post"].message == "chat.post" + suffix
    assert refused["deliver"].message == "chat.post" + suffix
    assert refused["title"].message == "sessions.create" + suffix
    assert refused["show_view"].message == "chat.show" + suffix


def test_the_run_is_the_only_session_and_has_nothing_waiting_or_shown(tmp_path: Path):
    backend = _backend(tmp_path)
    backend.broadcast(msg.Info(text="nobody is listening"))

    assert backend.default_session() == "s-1"
    assert backend.live_sessions() == ["s-1"]
    assert backend.views("s-1") == []
    assert backend.waiting_approvals("s-1") == []
    assert backend.project_dir_of("anything") == tmp_path
    assert backend.project_dir == tmp_path
    activity = backend.activity()
    assert activity.state is ActivityState.IDLE
    assert activity.suspendable and activity.sessions == []


def test_the_scratchpad_the_backend_offers_is_the_runs_own(tmp_path: Path):
    backend = _backend(tmp_path)
    backend.client_state.set_value("c", "k", "v")

    assert backend.client_state.get_for_client("c").values == {"k": "v"}
    assert _backend(tmp_path).client_state.get_for_client("c").values == {}


# ── Starting and stopping the run's extensions ────────────────────


class Mcp:
    def __init__(self) -> None:
        self.configs: dict[str, Any] = {}
        self.disconnects = 0
        self.extension_request_meta: Any = None

    def register_server(self, config: Any) -> None:
        self.configs[config.name] = config

    async def connect(self, name: str) -> Any:
        return MCPConnectResult.failure("its command was not found")

    async def disconnect_one(self, name: str) -> bool:
        return False

    async def disconnect_all(self) -> None:
        self.disconnects += 1

    def get_client(self, name: str) -> Any:
        return None


class Session:
    def __init__(self, tmp: Path) -> None:
        self.settings = SimpleNamespace(
            storage=SimpleNamespace(data_dir=str(tmp / "data")),
            auth=SimpleNamespace(credentials_file=str(tmp / "credentials.json")),
            api_url=None,
            group_policy=SimpleNamespace(trusted_keys=[], required=False),
        )
        self.project_dir = tmp / "project"
        self.project_dir.mkdir()
        self.session_id = "s-1"
        self.mcp_manager = Mcp()
        self.prompt_contributions = PromptContributions()
        self.prompt_loader = PromptLoader(PromptFragments())
        self.rebuilt = 0

    def reload_group_agents(self) -> bool:
        self.rebuilt += 1
        return True


def _bundle(tmp: Path, extension_id: str, hooks: list[str] | None = None) -> Path:
    bundle = tmp / extension_id
    bundle.mkdir()
    permissions: dict[str, Any] = {"hooks": hooks} if hooks else {}
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "id": extension_id,
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "name": extension_id,
                "trust": "unsigned",
                "dev": {"command": ["no-such-command"]},
                "permissions": permissions,
            }
        )
    )
    return bundle


def _manifest(bundle: Path) -> ExtensionManifest:
    return ExtensionManifest.model_validate(json.loads((bundle / "manifest.json").read_text()))


async def test_stop_before_start_and_twice_does_nothing(tmp_path: Path):
    session = Session(tmp_path)
    extensions = HeadlessExtensions(session)

    await extensions.stop()
    await extensions.start()  # nothing installed
    await extensions.stop()
    await extensions.stop()

    assert session.mcp_manager.disconnects == 0


async def test_a_started_run_wires_the_person_rebuilds_its_agents_and_stop_lets_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    ExtensionStore().add(_bundle(tmp_path, "acme.guard", ["PreToolUse"]))
    session = Session(tmp_path)
    extensions = HeadlessExtensions(session)

    await extensions.start()
    try:
        assert session.rebuilt == 1
        assert session.mcp_manager.extension_request_meta is not None
        assert extension_hook_gate.hooked(HookEvent.PRE_TOOL_USE)
    finally:
        await extensions.stop()

    assert session.mcp_manager.disconnects == 1
    assert not extension_hook_gate.hooked(HookEvent.PRE_TOOL_USE)


async def test_an_extension_that_hangs_past_the_bound_fails_while_a_ready_one_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    store = ExtensionStore()
    guard = _bundle(tmp_path, "acme.guard", ["PreToolUse"])
    store.add(guard)
    ready = _bundle(tmp_path, "acme.ready")
    broken = _bundle(tmp_path, "acme.broken")
    store.add(ready)
    store.add(broken)
    monkeypatch.setattr(HeadlessExtensions, "START_TIMEOUT_S", 0.2)
    extensions = HeadlessExtensions(Session(tmp_path))

    async def start_installed(launcher: ExtensionLauncher) -> None:
        registry = extensions.registry
        registry.install(_manifest(guard), state=ExtensionState.PREPARING)
        registry.install(_manifest(ready), state=ExtensionState.READY)
        registry.install(_manifest(broken), state=ExtensionState.FAILED, reason="no manifest")
        await asyncio.sleep(3600)

    monkeypatch.setattr(ExtensionLauncher, "start_installed", start_installed)
    caplog.set_level(logging.WARNING, logger="igni_core.backend.headless_extensions")

    await extensions.start()
    try:
        states = {
            i: (e.state, e.reason)
            for i in extensions.registry.ids()
            if (e := extensions.registry.get(i))
        }
        verdict = await extension_hook_gate.ask(
            PreToolUseValue(session_id="s-1", tool_name="run_shell_command")
        )
    finally:
        await extensions.stop()

    assert states == {
        "acme.guard": (ExtensionState.FAILED, "it did not start within 0.2s"),
        "acme.ready": (ExtensionState.READY, ""),
        "acme.broken": (ExtensionState.FAILED, "no manifest"),
    }
    assert not verdict.allowed
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("acme.guard" in w and "it did not start within 0.2s" in w for w in warned)
    assert any("acme.broken" in w and "no manifest" in w for w in warned)
    assert not any("acme.ready" in w for w in warned)
