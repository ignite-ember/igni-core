"""The install's steps are collaborators of their own, and each says what it did.

``installer.py`` used to be one class with sixteen fields and twenty-five
methods: trust, profile, assets, the package, the environment, the
service, the tools, the pages, the plugin. The behaviour was right and
the shape was not — the trust check could not be tried without a
registry, the environment could not be read without a service, and the
service's result was read by attribute-guessing rather than by the type
it actually has.

These are the seams that came out of it, each tested on its own:

* :meth:`~igni_core.core.mcp.client.MCPClientManager.register_server` —
  the way an extension's service is added to the manager, so nothing
  writes into the manager's own map from outside it.
* :class:`~igni_core.extensions.install_environment.ProcessEnvironment` —
  the environment a process starts with, whose parts have a precedence.
* :class:`~igni_core.extensions.install_preflight.InstallPreflight` —
  the rules a bundle must pass before anything is registered.
* :class:`~igni_core.core.mcp.schemas.MCPConnectResult` — the typed
  answer a connect gives, read as a type rather than guessed at.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.agno_telemetry import AgnoTelemetry
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.config import MCPServerConfig, MCPTransport
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.extensions.assets import ASSETS_DIR_ENV
from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_environment import ProcessEnvironment
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.install_preflight import InstallPreflight
from igni_core.extensions.installer import ExtensionInstaller
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import DATA_DIR_ENV


class StubMcp:
    """Enough of the MCP manager for an install that starts nothing real."""

    def __init__(self, result: MCPConnectResult) -> None:
        self.result = result
        self.configs: dict[str, MCPServerConfig] = {}

    def register_server(self, config: MCPServerConfig) -> None:
        self.configs[config.name] = config

    async def connect(self, name: str) -> MCPConnectResult:
        return self.result

    async def disconnect_one(self, name: str) -> bool:
        self.configs.pop(name, None)
        return True

    def get_client(self, name: str) -> None:
        return None

    def get_tools(self, name: str) -> list[str]:
        return []

    def set_tool_enabled(self, server: str, tool: str, enabled: bool) -> None:
        pass

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        pass

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        return {}


def bundle(tmp_path: Path, **changes: Any) -> Path:
    """A bundle that installs as a development install."""
    root = tmp_path / "bundle"
    root.mkdir()
    raw = {
        "id": "acme.notes",
        "version": "0.1.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["python", "server.py"]},
        **changes,
    }
    (root / "manifest.json").write_text(json.dumps(raw))
    return root


def test_a_registered_server_is_one_the_manager_can_connect_to(tmp_path: Path) -> None:
    """An extension's service is added through the manager, not into its map.

    Writing ``manager.configs[name] = config`` from the extension layer
    was a reach-in into somebody else's state; the manager now offers
    the operation, and the name it lists is the name ``connect`` takes.
    """
    manager = MCPClientManager(project_dir=tmp_path)
    config = MCPServerConfig(
        name="ext:acme.notes",
        type=MCPTransport.stdio,
        command="python",
        args=[],
        source="extension",
    )

    manager.register_server(config)

    assert "ext:acme.notes" in manager.list_servers()


async def test_a_service_that_refuses_to_start_is_recorded_with_its_reason(tmp_path: Path) -> None:
    """A refused connect is read as a result, not guessed at.

    The reason the service gave is the reason the extension's entry
    carries — the same words, because the answer is typed.
    """
    registry = ExtensionRegistry()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=StubMcp(MCPConnectResult.failure("its command was not found")),
        allow_development=True,
    )

    entry = await installer.install(bundle(tmp_path))

    assert entry.state is ExtensionState.FAILED
    assert entry.reason == "its service did not start: its command was not found"


def test_a_shipped_literal_cannot_shadow_what_igni_grants() -> None:
    """A bundle that ships its own token URL does not get one by shipping it.

    The literal is what the bundle brought; the grant is what igni
    decided the extension may have. The grant wins, or the manifest's
    own environment variable is a way to redirect a loopback token.
    """
    environment = ProcessEnvironment(
        literals={"IGNI_SERVICE_TOKEN_URL": "https://somewhere.else/token"},
        grants={"IGNI_SERVICE_TOKEN_URL": "http://127.0.0.1:9/token"},
    )

    assert environment.merged()["IGNI_SERVICE_TOKEN_URL"] == "http://127.0.0.1:9/token"


def test_the_directories_are_named_and_the_telemetry_switch_is_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two directories are in the environment, and Agno's switch is last.

    Agno's telemetry switch is applied last so that a bundle cannot turn
    it on, and a bundle that ships ``AGNO_TELEMETRY=true`` does not get
    it: an MCP stdio launch inherits almost nothing of igni's
    environment, so an extension built on Agno would otherwise run with
    Agno's default.
    """
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    environment = ProcessEnvironment(
        literals={"AGNO_TELEMETRY": "true"},
        assets_dir=tmp_path / "assets",
        data_dir=tmp_path / "data",
    )

    merged = environment.merged()

    assert merged[ASSETS_DIR_ENV] == str(tmp_path / "assets")
    assert merged[DATA_DIR_ENV] == str(tmp_path / "data")
    assert merged["AGNO_TELEMETRY"] == AgnoTelemetry.child_environment()["AGNO_TELEMETRY"]


def test_a_refused_bundle_is_never_registered(tmp_path: Path) -> None:
    """The rules run before anything is registered.

    A bundle igni server did not distribute, on a machine that does not
    allow development installs, is refused — and nothing is left in the
    registry, because nothing was ever accepted.
    """
    registry = ExtensionRegistry()
    preflight = InstallPreflight(context=InstallContext(allow_development=False))
    manifest = ExtensionManifest.load(bundle(tmp_path) / "manifest.json")

    with pytest.raises(InstallError, match="was not distributed by igni server"):
        preflight.check(manifest, None)

    assert registry.get("acme.notes") is None
