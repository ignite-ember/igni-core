"""What :class:`ExtensionStartup` connects, beyond which extensions it starts.

``test_the_backend_starts_and_syncs_extensions`` pins the launch gate and
the sync schedule. These pin the rest of the wiring a user depends on:
every call into an extension is for the person signed in here, an
extension's page can ask for its service token, one token exchange
serves them all, the air gap narrows the sandbox, one extension that
will not start does not stop the backend, and stopping is safe at any
point.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.backend import extension_startup as startup_module
from igni_core.backend.extension_startup import ExtensionStartup, ExtensionStartupContext
from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.backend.igni_server_access import IgniServerAccess
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.pool_rpcs import PageTokenRpc
from igni_core.core.config.settings import Settings
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.core.plugins.installer import PluginInstaller
from igni_core.core.prompts import PromptFragments, PromptLoader
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.extensions.distribution.sync import ExtensionDistribution
from igni_core.extensions.registry import ExtensionState
from igni_core.extensions.sandbox import ExtensionSandbox
from igni_core.extensions.service_token import ServiceTokenEndpoint
from igni_core.extensions.store import ExtensionStore
from tests.group_policy_signing import PERSON, sign_in


class _Mcp:
    def __init__(self) -> None:
        self.extension_request_meta: Any = None
        self.refuse: set[str] = set()

    def register_server(self, config: Any) -> None:
        return None

    async def connect(self, name: str) -> Any:
        if name in self.refuse:
            raise RuntimeError("the process would not start")
        return MCPConnectResult(ok=True)

    async def disconnect_one(self, name: str) -> None:
        return None

    def get_client(self, name: str) -> None:
        return None

    def get_tools(self, name: str) -> dict[str, Any]:
        return {}

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        return None

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        return {}


class _NoBackend:
    async def login(self, on_status: Any = None) -> Any:
        raise AssertionError("not used")

    def reload_cloud_credentials(self) -> None:
        return None


class _Orchestrator:
    def __init__(self) -> None:
        self.mounted: list[Any] = []

    def mount(self, rpc: Any) -> None:
        self.mounted.append(rpc)


class _Access(IgniServerAccess):
    """Counts the token exchanges it hands out."""

    exchanges = 0

    def token_exchange(self):
        type(self).exchanges += 1
        return super().token_exchange()


class Rig:
    def __init__(self, tmp_path: Path, *, air_gapped: bool = False) -> None:
        from igni_core.extensions.contributions import ExtensionContributions
        from igni_core.extensions.health import ExtensionHealth
        from igni_core.extensions.host.hooks import ExtensionHooks
        from igni_core.extensions.host.processes import ExtensionProcesses
        from igni_core.extensions.origin import ExtensionOrigins
        from igni_core.extensions.registry import ExtensionRegistry
        from igni_core.extensions.rpc import ExtensionRpc

        self.tmp = tmp_path
        self.mcp = _Mcp()
        self.orchestrator = _Orchestrator()
        self.credentials = tmp_path / "credentials.json"
        self.settings = Settings.model_validate(
            {
                "api_url": "",
                "air_gapped": air_gapped,
                "auth": {"credentials_file": str(self.credentials)},
                "storage": {"data_dir": str(tmp_path / "data")},
            }
        )
        rpc = ExtensionRpc(ExtensionRegistry())
        prompts = PromptContributions()
        hooks = ExtensionHooks(registry=rpc.registry)
        processes = ExtensionProcesses()
        self.runtime = ExtensionsRuntime(
            rpc=rpc,
            origins=ExtensionOrigins(),
            health=ExtensionHealth(
                registry=rpc.registry, rpc=rpc, mcp=self.mcp, processes=processes
            ),
            contributions=ExtensionContributions(
                registry=rpc.registry,
                rpc=rpc,
                prompt_loader=PromptLoader(PromptFragments()),
                prompts=prompts,
                hooks=hooks,
            ),
            prompts=prompts,
            hooks=hooks,
            processes=processes,
        )
        self.login = LoginCoordinator(backend=_NoBackend(), push_bridge=None)  # type: ignore[arg-type]
        self.context = ExtensionStartupContext(
            settings=self.settings,
            project_dir=tmp_path / "project",
            plugins_dir=PluginInstaller(data_dir=self.settings.storage.data_dir).plugins_dir,
            store=ExtensionStore(),
            access=_Access(credentials_file=str(self.credentials), api_url=lambda: ""),
            mcp=self.mcp,
            login=self.login,
            orchestrator=self.orchestrator,  # type: ignore[arg-type]
        )
        self.startup = ExtensionStartup(runtime=self.runtime, context=self.context)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("IGNI_DEV_EXTENSIONS", raising=False)
    _Access.exchanges = 0
    return Rig(tmp_path)


def _dev_install(tmp_path: Path, extension_id: str) -> None:
    source = tmp_path / f"source-{extension_id}"
    source.mkdir()
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "id": extension_id,
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


@pytest.mark.asyncio
async def test_before_start_there_is_no_endpoint_and_no_sync_and_stopping_is_safe(rig: Rig):
    assert rig.startup.service_tokens is None
    assert rig.startup.distribution is None

    await rig.startup.stop()


@pytest.mark.asyncio
async def test_after_start_the_endpoint_and_the_sync_exist(rig: Rig):
    await rig.startup.start()
    try:
        assert isinstance(rig.startup.service_tokens, ServiceTokenEndpoint)
        assert isinstance(rig.startup.distribution, ExtensionDistribution)
    finally:
        await _settle(rig.startup)
        await rig.startup.stop()


@pytest.mark.asyncio
async def test_stop_stops_the_sync_and_the_token_endpoint(rig: Rig, monkeypatch):
    stopped: list[str] = []
    real_sync, real_tokens = ExtensionDistribution.stop, ServiceTokenEndpoint.stop

    async def sync_stop(self):
        stopped.append("sync")
        await real_sync(self)

    async def tokens_stop(self):
        stopped.append("tokens")
        await real_tokens(self)

    monkeypatch.setattr(ExtensionDistribution, "stop", sync_stop)
    monkeypatch.setattr(ServiceTokenEndpoint, "stop", tokens_stop)
    await rig.startup.start()
    await _settle(rig.startup)

    await rig.startup.stop()

    assert sorted(stopped) == ["sync", "tokens"]


@pytest.mark.asyncio
async def test_every_call_into_an_extension_is_for_the_person_signed_in_here(rig: Rig):
    sign_in(rig.credentials)
    await rig.startup.start()
    try:
        meta = rig.mcp.extension_request_meta()
        assert meta is not None
        assert PERSON in json.dumps(meta)
        assert rig.runtime.hooks._principal() is not None
    finally:
        await _settle(rig.startup)
        await rig.startup.stop()


@pytest.mark.asyncio
async def test_with_nobody_signed_in_there_is_no_principal(rig: Rig):
    await rig.startup.start()
    try:
        assert rig.mcp.extension_request_meta() is None
        assert rig.runtime.hooks._principal() is None
    finally:
        await _settle(rig.startup)
        await rig.startup.stop()


@pytest.mark.asyncio
async def test_signing_in_after_start_is_seen_by_the_next_call(rig: Rig):
    await rig.startup.start()
    try:
        assert rig.mcp.extension_request_meta() is None
        sign_in(rig.credentials)
        assert PERSON in json.dumps(rig.mcp.extension_request_meta())
    finally:
        await _settle(rig.startup)
        await rig.startup.stop()


@pytest.mark.asyncio
async def test_the_page_token_rpc_is_mounted_on_the_orchestrator(rig: Rig):
    await rig.startup.start()
    try:
        [mounted] = rig.orchestrator.mounted
        assert isinstance(mounted, PageTokenRpc)
    finally:
        await _settle(rig.startup)
        await rig.startup.stop()


@pytest.mark.asyncio
async def test_one_token_exchange_serves_an_extension_and_its_page(rig: Rig):
    await rig.startup.start()
    try:
        assert _Access.exchanges == 1
    finally:
        await _settle(rig.startup)
        await rig.startup.stop()


@pytest.mark.asyncio
async def test_the_installer_gets_the_sandbox_made_from_the_settings(rig: Rig, monkeypatch):
    made: list[tuple[Settings, ExtensionSandbox]] = []
    real = ExtensionSandbox.for_settings.__func__  # type: ignore[attr-defined]

    def spy(cls, settings):
        sandbox = real(cls, settings)
        made.append((settings, sandbox))
        return sandbox

    monkeypatch.setattr(ExtensionSandbox, "for_settings", classmethod(spy))
    handed: list[Any] = []
    real_installer = startup_module.ExtensionInstaller

    class Spy(real_installer):  # type: ignore[valid-type, misc]
        def __init__(self, **kwargs: Any) -> None:
            handed.append(kwargs["sandbox"])
            super().__init__(**kwargs)

    monkeypatch.setattr(startup_module, "ExtensionInstaller", Spy)
    await rig.startup.start()
    try:
        [(settings, sandbox)] = made
        assert settings is rig.settings
        assert handed == [sandbox]
    finally:
        await _settle(rig.startup)
        await rig.startup.stop()


@pytest.mark.asyncio
async def test_an_extension_that_will_not_start_does_not_stop_the_backend(rig: Rig, monkeypatch):
    _dev_install(rig.tmp, "acme.broken")
    _dev_install(rig.tmp, "acme.fine")
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    rig.mcp.refuse = {"ext:acme.broken"}

    await rig.startup.start()
    try:
        await _settle(rig.startup)
        broken = rig.runtime.registry.get("acme.broken")
        fine = rig.runtime.registry.get("acme.fine")
        assert fine is not None and fine.state is ExtensionState.READY
        assert broken is None or broken.state is not ExtensionState.READY
    finally:
        await rig.startup.stop()


def test_the_context_is_frozen(rig: Rig):
    with pytest.raises(ValidationError):
        rig.context.project_dir = Path("/elsewhere")  # type: ignore[misc]
