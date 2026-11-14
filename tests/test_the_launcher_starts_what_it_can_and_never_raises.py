"""Starting and stopping stored extensions: one that will not start costs the rest nothing.

The launcher is the one path by which boot and the distribution sync
start an extension, so a bundle with an unreadable manifest, one that
the trust check refuses, one that fails to start and one whose
removal raises are all logged and the work goes on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.extensions.installer import ExtensionInstaller
from igni_core.extensions.launcher import ExtensionLauncher
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore


class FakeMcp:
    def __init__(self, result: Any = None) -> None:
        self.result = result or MCPConnectResult.success(None)

    def register_server(self, config: Any) -> None:
        pass

    async def connect(self, name: str) -> Any:
        return self.result

    async def disconnect_one(self, name: str) -> bool:
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


class Contributions:
    """Records what the registry held for an extension when its bundle was noted."""

    def __init__(self, registry: ExtensionRegistry) -> None:
        self.registry = registry
        self.noted: list[tuple[str, Path, bool]] = []

    def note_bundle(self, extension_id: str, bundle: Path) -> None:
        self.noted.append((extension_id, bundle, self.registry.get(extension_id) is None))


def add_bundle(store_root: Path, extension_id: str, *, valid: bool = True) -> Path:
    root = store_root / extension_id
    root.mkdir(parents=True)
    raw: dict[str, Any] = {"id": extension_id}
    if valid:
        raw = {
            "id": extension_id,
            "version": "0.1.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
        }
    (root / "manifest.json").write_text(json.dumps(raw))
    return root


def launcher_over(
    tmp_path: Path, *, allow_development: bool = True, result: Any = None
) -> tuple[ExtensionLauncher, ExtensionRegistry, Contributions, ExtensionInstaller]:
    registry = ExtensionRegistry()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=FakeMcp(result),
        allow_development=allow_development,
        tool_hints=ToolHintsRegistry(),
    )
    contributions = Contributions(registry)
    launcher = ExtensionLauncher(
        store=ExtensionStore(tmp_path / "store"),
        installer=installer,
        registry=registry,
        contributions=contributions,  # type: ignore[arg-type]
    )
    return launcher, registry, contributions, installer


async def test_boot_starts_every_stored_bundle_and_skips_one_it_cannot_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store_root = tmp_path / "store"
    add_bundle(store_root, "acme.a")
    add_bundle(store_root, "acme.b", valid=False)
    add_bundle(store_root, "acme.c")
    launcher, registry, _, _ = launcher_over(tmp_path)

    with caplog.at_level(logging.ERROR):
        await launcher.start_installed()

    assert registry.get("acme.a") is not None
    assert registry.get("acme.b") is None
    assert registry.get("acme.c") is not None
    assert "acme.b/manifest.json is not a valid manifest" in caplog.text


async def test_an_empty_store_starts_nothing(tmp_path: Path) -> None:
    launcher, registry, _, _ = launcher_over(tmp_path)

    await launcher.start_installed()

    assert registry.get("acme.a") is None


async def test_the_bundle_is_noted_before_its_install_begins(tmp_path: Path) -> None:
    bundle = add_bundle(tmp_path / "store", "acme.a")
    launcher, registry, contributions, _ = launcher_over(tmp_path)

    await launcher.activate("acme.a", bundle)

    assert contributions.noted == [("acme.a", bundle, True)]


async def test_activating_a_running_extension_replaces_it(tmp_path: Path) -> None:
    bundle = add_bundle(tmp_path / "store", "acme.a")
    launcher, registry, _, _ = launcher_over(tmp_path)
    await launcher.activate("acme.a", bundle)
    first = registry.get("acme.a")

    await launcher.activate("acme.a", bundle)

    second = registry.get("acme.a")
    assert second is not None and second is not first
    assert second.state is ExtensionState.READY


async def test_an_install_refused_before_registration_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bundle = add_bundle(tmp_path / "store", "acme.a")
    launcher, registry, _, _ = launcher_over(tmp_path, allow_development=False)

    with caplog.at_level(logging.ERROR):
        await launcher.activate("acme.a", bundle)

    assert registry.get("acme.a") is None
    assert "was not installed" in caplog.text


async def test_an_extension_that_does_not_come_up_is_a_logged_warning_with_its_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bundle = add_bundle(tmp_path / "store", "acme.a")
    launcher, registry, _, _ = launcher_over(
        tmp_path, result=MCPConnectResult.failure("its command was not found")
    )

    with caplog.at_level(logging.WARNING):
        await launcher.activate("acme.a", bundle)

    entry = registry.get("acme.a")
    assert entry is not None and entry.state is ExtensionState.FAILED
    assert "extension acme.a is FAILED: its service did not start: its command was not found" in (
        caplog.text
    )


async def test_deactivating_stops_a_running_extension(tmp_path: Path) -> None:
    bundle = add_bundle(tmp_path / "store", "acme.a")
    launcher, registry, _, _ = launcher_over(tmp_path)
    await launcher.activate("acme.a", bundle)

    await launcher.deactivate("acme.a")

    assert registry.get("acme.a") is None


async def test_deactivating_never_raises(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    launcher, _, _, installer = launcher_over(tmp_path)

    async def broken(extension_id: str) -> bool:
        raise RuntimeError("cannot stop")

    installer.uninstall = broken  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR):
        await launcher.deactivate("acme.a")

    assert "stopping extension acme.a failed" in caplog.text
