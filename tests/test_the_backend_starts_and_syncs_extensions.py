"""The backend's half: start what is stored, then sync in the background.

:class:`ExtensionStartup` is where the store, the installer, the token
endpoint and the distribution sync meet. These check the wiring rather
than the parts — each part has its own file:

* a stored development install starts only with ``IGNI_DEV_EXTENSIONS``;
* the sync is scheduled at start-up, never awaited by it;
* a sign-in schedules another.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.extension_startup import ExtensionStartup, ExtensionStartupContext
from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.backend.igni_server_access import IgniServerAccess
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.core.config.settings import Settings
from igni_core.core.plugins.installer import PluginInstaller
from igni_core.core.prompts import PromptFragments, PromptLoader
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.extensions.contributions import ExtensionContributions
from igni_core.extensions.health import ExtensionHealth
from igni_core.extensions.host.hooks import ExtensionHooks
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore


class StubMcp:
    def __init__(self) -> None:
        self.configs: dict[str, Any] = {}
        self.extension_request_meta: Any = None

    async def connect(self, name: str) -> Any:
        return SimpleNamespace(ok=True, reason="")

    async def disconnect_one(self, name: str) -> None:
        self.configs.pop(name, None)

    def get_client(self, name: str) -> None:
        return None

    def get_tools(self, name: str) -> dict[str, Any]:
        return {}

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        pass

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        return {}


class NoBackend:
    async def login(self, on_status: Any = None) -> Any:
        raise AssertionError("not used")

    def reload_cloud_credentials(self) -> None:
        return None


class MountingOrchestrator:
    """Takes the pool RPCs the start-up mounts; serves nothing."""

    def __init__(self) -> None:
        self.mounted: list[Any] = []

    def mount(self, rpc: Any) -> None:
        self.mounted.append(rpc)


@pytest.fixture
def mcp() -> StubMcp:
    return StubMcp()


@pytest.fixture
def runtime(mcp: StubMcp) -> ExtensionsRuntime:
    rpc = ExtensionRpc(ExtensionRegistry())
    prompts = PromptContributions()
    hooks = ExtensionHooks(registry=rpc.registry)
    return ExtensionsRuntime(
        rpc=rpc,
        origins=ExtensionOrigins(),
        health=ExtensionHealth(registry=rpc.registry, rpc=rpc, mcp=mcp),
        contributions=ExtensionContributions(
            registry=rpc.registry,
            rpc=rpc,
            prompt_loader=PromptLoader(PromptFragments()),
            prompts=prompts,
            hooks=hooks,
        ),
        prompts=prompts,
        hooks=hooks,
    )


@pytest.fixture
def login() -> LoginCoordinator:
    return LoginCoordinator(backend=NoBackend(), push_bridge=None)  # type: ignore[arg-type]


@pytest.fixture
def startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mcp: StubMcp,
    runtime: ExtensionsRuntime,
    login: LoginCoordinator,
) -> ExtensionStartup:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("IGNI_DEV_EXTENSIONS", raising=False)
    credentials = str(tmp_path / "credentials.json")
    settings = Settings.model_validate(
        {
            "api_url": "",
            "auth": {"credentials_file": credentials},
            "storage": {"data_dir": str(tmp_path / "data")},
        }
    )
    return ExtensionStartup(
        runtime=runtime,
        context=ExtensionStartupContext(
            settings=settings,
            project_dir=tmp_path / "project",
            plugins_dir=PluginInstaller(data_dir=settings.storage.data_dir).plugins_dir,
            store=ExtensionStore(),
            access=IgniServerAccess(credentials_file=credentials, api_url=lambda: ""),
            mcp=mcp,
            login=login,
            orchestrator=MountingOrchestrator(),
        ),
    )


def _dev_install(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "id": "acme.mine",
                "version": "0.1.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["python", "server.py"]},
            }
        )
    )
    ExtensionStore().add(source)


async def _settle(startup: ExtensionStartup) -> None:
    assert startup.distribution is not None
    for task in list(startup.distribution._tasks):
        await task


async def test_a_stored_development_install_needs_the_flag(startup, runtime, tmp_path):
    _dev_install(tmp_path)
    await startup.start()
    await _settle(startup)
    assert runtime.registry.get("acme.mine") is None
    await startup.stop()


async def test_with_the_flag_it_starts(startup, runtime, tmp_path, monkeypatch):
    _dev_install(tmp_path)
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    await startup.start()
    await _settle(startup)
    entry = runtime.registry.get("acme.mine")
    assert entry is not None and entry.state is ExtensionState.READY, entry
    await startup.stop()


async def test_a_sync_is_scheduled_at_start_and_on_sign_in(startup, login):
    await startup.start()
    assert startup.distribution is not None
    assert startup.distribution._tasks, "start-up schedules a sync without waiting for it"
    await _settle(startup)

    login._notify_signed_in()
    assert startup.distribution._tasks, "a sign-in schedules another"
    await _settle(startup)
    await startup.stop()
