"""A session the pool opens in another folder runs on that folder's settings.

The pool's factory handed every session a copy of the settings the backend
booted with whenever the session's folder had the boot folder's trust state.
Those were merged from the boot folder's ``.igni`` files, so a window
working in folder B ran on folder A's project config: its permissions, MCP
servers, hooks. A folder other than the boot one now loads its own, with the
same loader and trust gate, and the command line's overrides on top.

These go through the real settings loader and the real trust service: the
folders' ``.igni/config.yaml`` files are the subject.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.schemas_pool import ProjectDirs
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.backend.session_runtime_factory import BootRuntime, SessionRuntimeFactory
from igni_core.backend.settings_loader import BackendSettingsLoader
from igni_core.backend.workspace_trust_controller import reload_settings
from igni_core.core.config.cli_overrides import (
    CliOverrides,
    DisplayOverrides,
    PermissionOverrides,
)
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.session_directories import SessionDirectoryStore
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator
from igni_core.core.workspace_trust import process_trust
from tests.shared_tables import SharedTables

pytestmark = pytest.mark.workspace_trust


class _Server:
    """Keeps the settings it was built with; nothing more."""

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        self.settings = settings
        self.session_id = kwargs["resume_session_id"]
        self.project_dir = kwargs["project_dir"]
        self.message_queue: list[str] = []
        self.broadcast_bus = BroadcastBus()
        self.supervisor = kwargs["supervisors"].supervisor
        self.gate = kwargs["gate"]

    async def startup(self) -> None:
        return None

    def wire_queue_hook(self, queue: Any) -> None:
        return None

    def wire_orchestrate_progress(self, callback: Any) -> None:
        return None

    def register_broadcast(self, callback: Any) -> None:
        self.broadcast_bus.register(callback)

    def attach_runtime(self, runtime: Any) -> None:
        return None


class _Transport:
    async def send(self, message: Any) -> None:
        return None


#: Folder A, the boot folder: verbose reasoning, the shell refused.
A_CONFIG = {"display": {"show_reasoning": True}, "permissions": {"shell_execute": "deny"}}
#: Folder B: routing shown, the shell and file writes allowed.
B_CONFIG = {
    "display": {"show_routing": True, "show_tool_calls": True},
    "permissions": {"shell_execute": "allow", "file_write": "allow"},
}


def _folder(path: Path, config: dict[str, Any]) -> Path:
    (path / ".igni").mkdir(parents=True)
    (path / ".igni" / "config.yaml").write_text(yaml.safe_dump(config))
    return path


def _factory(
    tmp_path: Path,
    boot_dir: Path,
    *,
    boot_settings: Settings | None = None,
    cli: CliOverrides | None = None,
) -> tuple[SessionRuntimeFactory, PoolDirectories, Settings]:
    settings = boot_settings if boot_settings is not None else reload_settings(boot_dir, cli)
    store = SessionDirectoryStore.from_locator(
        StorageLocator.from_settings(Settings(storage={"data_dir": str(tmp_path / "data")}))
    )
    dirs = PoolDirectories(store, ProjectDirs(project_dir=boot_dir))
    transport = _Transport()
    push = PushNotificationBridge(transport=transport, loop=None, queue=[])
    tables = SharedTables()
    wiring = PoolWiring(
        transport=transport,
        push_bridge=push,
        login=LoginCoordinator(backend=None, push_bridge=push),
        supervisors=SupervisorLocator(),
        gate=RunGate(),
        contributions=tables.contributions,
        prompt_loader=tables.prompt_loader,
        tool_hints=tables.tool_hints,
        extensions=tables.extensions,
    )
    factory = SessionRuntimeFactory(
        wiring=wiring,
        settings=settings,
        dirs=dirs,
        server_cls=_Server,  # type: ignore[arg-type]
        cli_overrides=cli,
    )
    return factory, dirs, settings


async def _settings_in(
    factory: SessionRuntimeFactory, dirs: PoolDirectories, session_id: str, folder: Path
) -> Settings:
    dirs.record(session_id, folder)
    runtime = await factory.create(session_id)
    return runtime.backend.settings


async def test_two_trusted_folders_each_run_on_their_own_project_settings(tmp_path) -> None:
    a = _folder(tmp_path / "a", A_CONFIG)
    b = _folder(tmp_path / "b", B_CONFIG)
    process_trust().trust(a, remember=False)
    process_trust().trust(b, remember=False)
    factory, dirs, _ = _factory(tmp_path, a)

    in_a = await _settings_in(factory, dirs, "sess-a", a)
    in_b = await _settings_in(factory, dirs, "sess-b", b)

    assert in_a.display.show_reasoning is True
    assert in_a.display.show_routing is False
    assert in_a.permissions.shell_execute == "deny"
    # B's own, and none of A's.
    assert in_b.display.show_routing is True
    assert in_b.display.show_reasoning is False
    assert in_b.permissions.shell_execute == "allow"
    assert in_b.permissions.file_write == "allow"


async def test_the_command_line_still_wins_in_another_folder(tmp_path) -> None:
    a = _folder(tmp_path / "a", A_CONFIG)
    b = _folder(tmp_path / "b", B_CONFIG)
    process_trust().trust(a, remember=False)
    process_trust().trust(b, remember=False)
    cli = CliOverrides(
        display=DisplayOverrides(show_tool_calls=False),
        permissions=PermissionOverrides(file_write="deny"),
    )
    factory, dirs, boot = _factory(tmp_path, a, cli=cli)

    in_b = await _settings_in(factory, dirs, "sess-b", b)

    assert boot.permissions.file_write == "deny"
    # B's file says allow and show; the flags say otherwise, and win.
    assert in_b.permissions.file_write == "deny"
    assert in_b.display.show_tool_calls is False
    # The rest is still B's own.
    assert in_b.display.show_routing is True
    assert in_b.permissions.shell_execute == "allow"


async def test_an_untrusted_folder_does_not_get_its_project_permissions(tmp_path) -> None:
    # Neither folder trusted: the same trust state as the boot folder.
    a = _folder(tmp_path / "a", A_CONFIG)
    b = _folder(tmp_path / "b", B_CONFIG)
    assert not process_trust().decide(a).trusted
    assert not process_trust().decide(b).trusted
    factory, dirs, _ = _factory(tmp_path, a)

    in_b = await _settings_in(factory, dirs, "sess-b", b)

    # What an untrusted repository may set is still B's...
    assert in_b.display.show_routing is True
    assert in_b.display.show_reasoning is False
    # ...but its permission rules are not.
    defaults = Settings().permissions
    assert in_b.permissions.shell_execute == defaults.shell_execute
    assert in_b.permissions.file_write == defaults.file_write


async def test_the_boot_folder_keeps_the_settings_it_booted_with(tmp_path, monkeypatch) -> None:
    a = _folder(tmp_path / "a", A_CONFIG)
    process_trust().trust(a, remember=False)
    # Unlike A's file: proof the boot folder is not read again.
    boot = Settings(display={"show_routing": True, "show_reasoning": False})
    reloaded: list[Path] = []
    monkeypatch.setattr(
        "igni_core.backend.session_runtime_factory.reload_settings",
        lambda project_dir, cli_overrides=None: reloaded.append(project_dir) or Settings(),
    )
    factory, dirs, _ = _factory(tmp_path, a, boot_settings=boot)

    in_a = await _settings_in(factory, dirs, "sess-a", a)
    # Spelled differently, still the boot folder.
    in_a_again = await _settings_in(factory, dirs, "sess-a2", tmp_path / "b" / ".." / "a")

    assert reloaded == []
    for settings in (in_a, in_a_again):
        assert settings is not boot
        assert settings == boot
        assert settings.display.show_routing is True
        assert settings.display.show_reasoning is False


# ── Where the overrides are kept ────────────────────────────────────


def test_the_backend_boots_with_its_command_line_overrides(tmp_path) -> None:
    a = _folder(tmp_path / "a", B_CONFIG)
    process_trust().trust(a, remember=False)
    cli = CliOverrides(permissions=PermissionOverrides(file_write="deny"))

    settings = BackendSettingsLoader(a, RunGate(), cli).load()

    assert settings.permissions.file_write == "deny"
    assert settings.permissions.shell_execute == "allow"


def test_the_pool_hands_its_factory_the_backend_s_overrides(tmp_path) -> None:
    """The factory re-applies what the orchestrator gives it, so the
    orchestrator must give it the launch's overrides."""
    cli = CliOverrides(permissions=PermissionOverrides(file_write="deny"))
    handed: dict[str, Any] = {}

    class _Stop(Exception):
        pass

    class _SpyFactory:
        def __init__(self, **kwargs: Any) -> None:
            handed.update(kwargs)
            raise _Stop

    project = tmp_path / "p"
    project.mkdir()
    backend = SimpleNamespace(session_id="boot", project_dir=project)

    with pytest.raises(_Stop):
        SessionOrchestrator.start(
            boot=BootRuntime(backend=backend, rpc_table={}, router=None, queue=[]),  # type: ignore[arg-type]
            wiring=None,  # type: ignore[arg-type]
            settings=Settings(storage={"data_dir": str(tmp_path / "data")}),
            dirs=ProjectDirs(project_dir=project),
            factory=_SpyFactory,  # type: ignore[arg-type]
            cli_overrides=cli,
        )

    assert handed["cli_overrides"] is cli
