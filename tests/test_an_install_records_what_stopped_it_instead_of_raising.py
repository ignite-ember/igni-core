"""What an install does when something in it cannot be done.

A bundle that is not installable at all raises, naming the file. A
packaging igni cannot honour, a service that will not start and pages
that cannot be shown are recorded on the extension's entry as FAILED
with the reason, and the entry stays in the registry for an
administrator to see. An unknown id uninstalls to ``False`` and
touches nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.installer import ExtensionInstaller
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc

READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


class FakeMcp:
    def __init__(self, tools: list[str] | None = None, reported: dict[str, Any] | None = None):
        self.configs: dict[str, Any] = {}
        self.tools = tools or []
        self.reported = reported or {}
        self.disconnected: list[str] = []

    def register_server(self, config: Any) -> None:
        self.configs[config.name] = config

    async def connect(self, name: str) -> Any:
        return MCPConnectResult.success(None)

    async def disconnect_one(self, name: str) -> bool:
        self.disconnected.append(name)
        return True

    def get_client(self, name: str) -> Any:
        return object()

    def get_tools(self, name: str) -> list[str]:
        return self.tools

    def set_tool_enabled(self, server: str, tool: str, enabled: bool) -> None:
        pass

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        pass

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        return self.reported


def make_bundle(tmp_path: Path, **changes: Any) -> Path:
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


def installer_over(mcp: FakeMcp) -> tuple[ExtensionInstaller, ExtensionRegistry]:
    registry = ExtensionRegistry()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        allow_development=True,
        tool_hints=ToolHintsRegistry(),
    )
    return installer, registry


async def test_a_manifest_that_does_not_validate_is_refused_naming_its_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"id": "acme.notes"}))
    installer, registry = installer_over(FakeMcp())

    with pytest.raises(InstallError, match="manifest.json is not a valid manifest: "):
        await installer.install(root)

    assert registry.get("acme.notes") is None


async def test_a_packaging_igni_cannot_honour_leaves_a_failed_entry_and_no_service(
    tmp_path: Path,
) -> None:
    mcp = FakeMcp()
    installer, registry = installer_over(mcp)
    abandoned: list[str] = []
    real_abandon = installer._runtime.abandon

    async def record(extension_id: str) -> None:
        abandoned.append(extension_id)
        await real_abandon(extension_id)

    installer._runtime.abandon = record  # type: ignore[method-assign]
    root = make_bundle(
        tmp_path,
        dev=None,
        sidecar={"image": "acme/notes:1", "mcp": {"transport": "stdio"}},
    )
    raw = json.loads((root / "manifest.json").read_text())
    raw.pop("dev")
    (root / "manifest.json").write_text(json.dumps(raw))

    entry = await installer.install(root)

    assert entry.state is ExtensionState.FAILED
    assert entry.reason is not None and "sidecar image (acme/notes:1)" in entry.reason
    assert registry.get("acme.notes") is entry
    assert mcp.configs == {}
    assert abandoned == ["acme.notes"]


async def test_an_endpoint_outside_its_egress_is_recorded_as_failed_not_raised(
    tmp_path: Path,
) -> None:
    installer, registry = installer_over(FakeMcp())
    root = make_bundle(
        tmp_path,
        endpoint={"url": "https://elsewhere.test/mcp"},
        permissions={"egress": ["svc.acme.test"]},
    )
    raw = json.loads((root / "manifest.json").read_text())
    raw.pop("dev")
    (root / "manifest.json").write_text(json.dumps(raw))

    entry = await installer.install(root)

    assert entry.state is ExtensionState.FAILED
    assert entry.reason is not None and "elsewhere.test" in entry.reason
    assert registry.get("acme.notes") is entry


async def test_pages_that_cannot_be_published_fail_a_working_service(tmp_path: Path) -> None:
    installer, registry = installer_over(FakeMcp())

    async def refuse(*args: Any) -> str:
        return "its pages could not be served"

    installer._runtime.publish = refuse  # type: ignore[method-assign]

    entry = await installer.install(make_bundle(tmp_path))

    assert entry.state is ExtensionState.FAILED
    assert entry.reason == "its pages could not be served"
    assert registry.get("acme.notes") is entry


async def test_what_its_server_contradicts_is_shown_as_a_problem_on_a_ready_extension(
    tmp_path: Path,
) -> None:
    server = "ext:acme.notes"
    mcp = FakeMcp(
        tools=[f"mcp_{server}_list"],
        reported={"list": {**READ, "readOnlyHint": False}},
    )
    installer, registry = installer_over(mcp)
    root = make_bundle(tmp_path, permissions={"tools": [{"name": "list", "annotations": READ}]})

    entry = await installer.install(root)

    assert entry.state is ExtensionState.READY
    assert len(entry.problems) >= 1
    assert entry.problems[0].startswith("its tool list reports itself as changes things")


async def test_uninstalling_an_unknown_id_is_false_and_stops_nothing(tmp_path: Path) -> None:
    mcp = FakeMcp()
    installer, registry = installer_over(mcp)

    assert await installer.uninstall("acme.ghost") is False
    assert mcp.disconnected == []


async def test_uninstalling_stops_the_service_and_forgets_the_entry(tmp_path: Path) -> None:
    mcp = FakeMcp()
    installer, registry = installer_over(mcp)
    await installer.install(make_bundle(tmp_path))

    assert await installer.uninstall("acme.notes") is True

    assert registry.get("acme.notes") is None
    assert mcp.disconnected == ["ext:acme.notes"]
