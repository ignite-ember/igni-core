"""How igni reaches an extension's service, and what of it the agent is offered.

The connection builds its configuration from the manifest alone, refuses
the packagings it cannot honour in words that name the way out, reports
a service that will not start as a reason rather than a raise, withholds
every tool the manifest does not declare, and lets the manifest's hints
(not the server's) decide how the permission modes treat a tool.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.mcp.config import MCPTransport
from igni_core.core.mcp.hints import ToolHints, ToolHintsRegistry
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.extensions import install_service
from igni_core.extensions.install_environment import ProcessEnvironment
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.install_service import ExtensionConnection, server_name
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.permissions import PermissionDenied, Permissions

READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
WRITE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}


def manifest_for(tmp_path: Path, **changes: Any) -> ExtensionManifest:
    raw = {
        "id": "acme.notes",
        "version": "0.1.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        **changes,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(raw))
    return ExtensionManifest.load(path)


class FakeMcp:
    """The slice of the manager an install talks to, recording what it was told."""

    def __init__(self) -> None:
        self.registered: list[Any] = []
        self.tools: list[str] = []
        self.disabled: list[tuple[str, str, bool]] = []
        self.confirmation: dict[str, set[str]] = {}
        self.reported: dict[str, Any] = {}
        self.connect_result: Any = MCPConnectResult.success(None)
        self.connect_raises: BaseException | None = None
        self.disconnect_raises: BaseException | None = None
        self.client: Any = object()

    def register_server(self, config: Any) -> None:
        self.registered.append(config)

    async def connect(self, name: str) -> Any:
        if self.connect_raises is not None:
            raise self.connect_raises
        return self.connect_result

    async def disconnect_one(self, name: str) -> bool:
        if self.disconnect_raises is not None:
            raise self.disconnect_raises
        return True

    def get_client(self, name: str) -> Any:
        return self.client

    def get_tools(self, name: str) -> list[str]:
        return self.tools

    def set_tool_enabled(self, server: str, tool: str, enabled: bool) -> None:
        self.disabled.append((server, tool, enabled))

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        self.confirmation[server] = tools

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        return self.reported


def connection(mcp: FakeMcp, hints: ToolHintsRegistry | None = None) -> ExtensionConnection:
    rpc = SimpleNamespace(bound={}, bind=None, unbind=None)
    rpc.bind = lambda ext, service: rpc.bound.__setitem__(ext, service)
    rpc.unbind = lambda ext: rpc.bound.pop(ext, None)
    return ExtensionConnection(
        mcp=mcp,  # type: ignore[arg-type]
        rpc=rpc,  # type: ignore[arg-type]
        tool_hints=hints if hints is not None else ToolHintsRegistry(),
    )


def test_an_extension_server_is_named_so_it_cannot_shadow_a_users_own() -> None:
    assert server_name("github") == "ext:github"


def test_a_python_extension_runs_its_installed_entry_point_in_its_bundle(tmp_path: Path) -> None:
    manifest = manifest_for(
        tmp_path,
        python={"package": "p", "entry": "e", "mcp": {"transport": "stdio"}},
    )
    environment = ProcessEnvironment(literals={"A": "1"}, grants={"B": "2"})

    config = connection(FakeMcp()).config_for(
        manifest,
        tmp_path,
        Permissions(manifest),
        tmp_path / "bin" / "notes",
        environment,
    )

    assert config.name == "ext:acme.notes"
    assert config.type is MCPTransport.stdio
    assert config.command == str(tmp_path / "bin" / "notes")
    assert config.args == []
    assert config.cwd == str(tmp_path)
    assert config.env is not None and config.env["A"] == "1" and config.env["B"] == "2"


def test_a_python_extension_whose_package_was_not_installed_is_refused(tmp_path: Path) -> None:
    manifest = manifest_for(
        tmp_path, python={"package": "p", "entry": "e", "mcp": {"transport": "stdio"}}
    )

    with pytest.raises(InstallError, match="acme.notes: its package was not installed"):
        connection(FakeMcp()).config_for(
            manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
        )


def test_a_dev_extension_runs_its_command_where_the_bundle_says(tmp_path: Path) -> None:
    manifest = manifest_for(
        tmp_path,
        dev={
            "command": ["python", "server.py", "--flag"],
            "cwd": "src",
            "env": {"FROM_BUNDLE": "yes", "SHARED": "bundle"},
        },
    )
    environment = ProcessEnvironment(grants={"SHARED": "grant"})

    config = connection(FakeMcp()).config_for(
        manifest, tmp_path, Permissions(manifest), None, environment
    )

    assert config.command == "python"
    assert config.args == ["server.py", "--flag"]
    assert config.cwd == str(tmp_path / "src")
    assert config.env is not None
    assert config.env["FROM_BUNDLE"] == "yes"
    assert config.env["SHARED"] == "grant"


def test_a_dev_extension_without_a_cwd_runs_in_the_bundle_and_a_lone_command_has_no_args(
    tmp_path: Path,
) -> None:
    manifest = manifest_for(tmp_path, dev={"command": ["serve"]})

    config = connection(FakeMcp()).config_for(
        manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
    )

    assert config.command == "serve"
    assert config.args == []
    assert config.cwd == str(tmp_path)


def test_an_endpoint_extension_is_reached_over_sse_at_its_declared_host(tmp_path: Path) -> None:
    manifest = manifest_for(
        tmp_path,
        endpoint={"url": "https://svc.acme.test/mcp"},
        permissions={"egress": ["svc.acme.test"]},
    )

    config = connection(FakeMcp()).config_for(
        manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
    )

    assert config.type is MCPTransport.sse
    assert config.url == "https://svc.acme.test/mcp"
    assert config.name == "ext:acme.notes"


def test_an_endpoint_outside_its_declared_egress_is_refused(tmp_path: Path) -> None:
    manifest = manifest_for(
        tmp_path,
        endpoint={"url": "https://elsewhere.test/mcp"},
        permissions={"egress": ["svc.acme.test"]},
    )

    with pytest.raises(PermissionDenied, match="elsewhere.test"):
        connection(FakeMcp()).config_for(
            manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
        )


def test_a_sidecar_is_refused_naming_its_image_and_the_three_alternatives(tmp_path: Path) -> None:
    manifest = manifest_for(
        tmp_path, sidecar={"image": "acme/notes:1", "mcp": {"transport": "stdio"}}
    )

    with pytest.raises(InstallError) as caught:
        connection(FakeMcp()).config_for(
            manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
        )

    message = str(caught.value)
    assert "acme.notes is packaged as a sidecar image (acme/notes:1)" in message
    assert "'python'" in message and "'endpoint'" in message and "'dev'" in message


def test_a_manifest_with_no_sidecar_is_refused_with_a_question_mark_for_the_image(
    tmp_path: Path,
) -> None:
    manifest = manifest_for(tmp_path, dev={"command": ["serve"]})
    manifest = manifest.model_copy(update={"dev": None})

    with pytest.raises(InstallError, match=r"sidecar image \(\?\)"):
        connection(FakeMcp()).config_for(
            manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
        )


async def test_a_service_that_connects_registers_and_reports_nothing(tmp_path: Path) -> None:
    mcp = FakeMcp()
    manifest = manifest_for(tmp_path, dev={"command": ["serve"]})
    config = connection(mcp).config_for(
        manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
    )

    assert await connection(mcp).start(config) is None
    assert mcp.registered == [config]


async def test_a_service_that_raises_on_connect_is_a_reason_not_a_raise(tmp_path: Path) -> None:
    mcp = FakeMcp()
    mcp.connect_raises = RuntimeError("boom")
    manifest = manifest_for(tmp_path, dev={"command": ["serve"]})
    config = connection(mcp).config_for(
        manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
    )

    assert await connection(mcp).start(config) == "its service could not be started: boom"


async def test_a_service_that_refuses_without_a_reason_says_so(tmp_path: Path) -> None:
    mcp = FakeMcp()
    mcp.connect_result = MCPConnectResult.failure("")
    manifest = manifest_for(tmp_path, dev={"command": ["serve"]})
    config = connection(mcp).config_for(
        manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
    )

    assert await connection(mcp).start(config) == "its service did not start: no reason given"


async def test_a_service_that_never_answers_is_given_up_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp()

    async def never(name: str) -> Any:
        await asyncio.sleep(30)

    mcp.connect = never  # type: ignore[method-assign]
    monkeypatch.setattr(install_service, "START_TIMEOUT_S", 0.05)
    manifest = manifest_for(tmp_path, dev={"command": ["serve"]})
    config = connection(mcp).config_for(
        manifest, tmp_path, Permissions(manifest), None, ProcessEnvironment()
    )

    assert await connection(mcp).start(config) == "its service did not start within 0.05s"


def test_only_the_tools_the_manifest_declares_stay_offered(tmp_path: Path) -> None:
    mcp = FakeMcp()
    server = server_name("acme.notes")
    mcp.tools = [f"mcp_{server}_list", f"mcp_{server}_wipe"]
    manifest = manifest_for(
        tmp_path,
        dev={"command": ["serve"]},
        permissions={"tools": [{"name": "list", "annotations": READ}]},
    )

    connection(mcp).restrict_tools(manifest, server, Permissions(manifest))

    assert mcp.disabled == [(server, f"mcp_{server}_wipe", False)]


def test_restricting_tools_with_no_live_client_does_nothing(tmp_path: Path) -> None:
    mcp = FakeMcp()
    mcp.client = None
    server = server_name("acme.notes")
    mcp.tools = [f"mcp_{server}_wipe"]
    manifest = manifest_for(tmp_path, dev={"command": ["serve"]})

    connection(mcp).restrict_tools(manifest, server, Permissions(manifest))

    assert mcp.disabled == []


def hinted_manifest(tmp_path: Path) -> ExtensionManifest:
    return manifest_for(
        tmp_path,
        dev={"command": ["serve"]},
        permissions={
            "tools": [
                {"name": "list", "annotations": READ},
                {"name": "wipe", "annotations": WRITE},
            ]
        },
    )


async def test_the_manifests_hints_reach_the_registry_and_only_writes_ask_first(
    tmp_path: Path,
) -> None:
    mcp = FakeMcp()
    server = server_name("acme.notes")
    mcp.tools = [f"mcp_{server}_list", f"mcp_{server}_wipe", f"mcp_{server}_extra"]
    registry = ToolHintsRegistry()
    manifest = hinted_manifest(tmp_path)

    problems = await connection(mcp, registry).apply_hints(manifest, server, Permissions(manifest))

    assert problems == []
    assert mcp.confirmation[server] == {f"mcp_{server}_wipe"}
    assert registry.get(f"mcp_{server}_list") == ToolHints.model_validate(READ)
    assert registry.get(f"mcp_{server}_wipe") == ToolHints.model_validate(WRITE)
    assert registry.get(f"mcp_{server}_extra") is None


async def test_a_server_that_reports_other_hints_is_a_problem_and_the_manifest_applies(
    tmp_path: Path,
) -> None:
    mcp = FakeMcp()
    server = server_name("acme.notes")
    mcp.tools = [f"mcp_{server}_list", f"mcp_{server}_wipe"]
    mcp.reported = {"list": {**READ, "readOnlyHint": False}, "wipe": WRITE}
    registry = ToolHintsRegistry()
    manifest = hinted_manifest(tmp_path)

    problems = await connection(mcp, registry).apply_hints(manifest, server, Permissions(manifest))

    assert problems == [
        "its tool list reports itself as changes things, local, idempotent, and its "
        "manifest says read-only, local, idempotent; the manifest's hints apply"
    ]
    assert registry.get(f"mcp_{server}_list") == ToolHints.model_validate(READ)


async def test_a_server_that_reports_no_annotations_is_no_problem(tmp_path: Path) -> None:
    mcp = FakeMcp()
    server = server_name("acme.notes")
    mcp.tools = [f"mcp_{server}_list"]
    manifest = hinted_manifest(tmp_path)

    assert await connection(mcp).apply_hints(manifest, server, Permissions(manifest)) == []


def test_binding_routes_the_extensions_calls_to_its_service_and_unbinding_stops_it() -> None:
    mcp = FakeMcp()
    conn = connection(mcp)
    rpc = conn._rpc  # type: ignore[attr-defined]

    conn.bind("acme.notes", "ext:acme.notes")
    assert rpc.bound["acme.notes"].server_name == "ext:acme.notes"

    conn.unbind("acme.notes")
    assert "acme.notes" not in rpc.bound


async def test_disconnecting_swallows_a_failure_and_forgetting_drops_the_hints() -> None:
    mcp = FakeMcp()
    mcp.disconnect_raises = RuntimeError("already gone")
    registry = ToolHintsRegistry()
    server = server_name("acme.notes")
    registry.declare(server, {f"mcp_{server}_list": ToolHints.model_validate(READ)})
    conn = connection(mcp, registry)

    await conn.disconnect("acme.notes")
    conn.forget_hints("acme.notes")

    assert registry.get(f"mcp_{server}_list") is None
