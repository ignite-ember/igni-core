"""What runs for an installed extension, and the order it happens in.

The coordinator owns the order its four collaborators are used in:
prepare lays assets out before the package is resolved and the
environment built before the config; publish binds the calls before the
pages and unbinds them again when the pages fail; stop unbinds before it
disconnects. Each collaborator is a recorder writing to one shared log,
so the order is what is asserted, along with what flows from one step to
the next.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.core.mcp.config import MCPServerConfig, MCPTransport
from igni_core.extensions.assets import AssetError
from igni_core.extensions.install_environment import ProcessEnvironment
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.install_runtime import (
    ExtensionRuntime,
    PreparedExtension,
    ServiceStart,
)
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.permissions import Permissions
from igni_core.extensions.provenance import DistributionRecord
from igni_core.extensions.sandbox import Confinement
from tests.extension_distribution_helpers import manifest as raw_manifest


def load() -> ExtensionManifest:
    return ExtensionManifest.model_validate(raw_manifest())


def config(command: str = "acme-notes") -> MCPServerConfig:
    return MCPServerConfig(name="ext:acme.notes", type=MCPTransport.stdio, command=command)


class Collaborators:
    """All four, writing what they were asked to one log."""

    def __init__(self, tmp_path: Path) -> None:
        self.log: list[str] = []
        self.args: dict[str, Any] = {}
        self.tmp_path = tmp_path
        self.start_reason: str | None = None
        self.hints: list[str] = []
        self.pages_reason: str | None = None
        self.fail_assets: Exception | None = None

    def runtime(self) -> ExtensionRuntime:
        return ExtensionRuntime(
            environment=self.Environment(self),  # type: ignore[arg-type]
            process=self.Process(self),  # type: ignore[arg-type]
            connection=self.Connection(self),  # type: ignore[arg-type]
            pages=self.Pages(self),  # type: ignore[arg-type]
            plugin=self.Plugin(self),  # type: ignore[arg-type]
        )

    class Environment:
        def __init__(self, outer: Collaborators) -> None:
            self.o = outer

        def permissions(self, manifest: ExtensionManifest) -> Permissions:
            self.o.log.append("permissions")
            return Permissions(manifest)

        def service_url(self, manifest: ExtensionManifest, distribution: Any) -> str:
            self.o.log.append("service_url")
            return "https://notes.example.com"

        async def build(
            self, manifest: Any, permissions: Any, service_url: Any, assets_dir: Any = None
        ) -> ProcessEnvironment:
            self.o.log.append("build")
            self.o.args["build"] = (service_url, assets_dir)
            return ProcessEnvironment(data_dir=self.o.tmp_path / "data")

        def revoke(self, extension_id: str) -> None:
            self.o.log.append("revoke")

    class Process:
        def __init__(self, outer: Collaborators) -> None:
            self.o = outer

        async def lay_out_assets(self, manifest: Any, bundle: Any, distribution: Any) -> Path:
            self.o.log.append("lay_out_assets")
            if self.o.fail_assets is not None:
                raise self.o.fail_assets
            return bundle / "assets"

        async def python_entry(self, manifest: Any, bundle: Any, distribution: Any) -> Path:
            self.o.log.append("python_entry")
            return bundle / "bin" / "acme-notes"

        async def confine(self, manifest: Any, cfg: MCPServerConfig, **kwargs: Any) -> Any:
            self.o.log.append("confine")
            self.o.args["confine"] = (cfg, kwargs)
            return cfg.model_copy(update={"command": "confined"})

        async def release(self, extension_id: str) -> None:
            self.o.log.append("release")

        def status(self, extension_id: str) -> Confinement | None:
            self.o.log.append("status")
            return Confinement(extension_id=extension_id, reason="no sandbox here")

    class Connection:
        def __init__(self, outer: Collaborators) -> None:
            self.o = outer

        def config_for(
            self, manifest: Any, bundle: Any, permissions: Any, entry_point: Any, environment: Any
        ) -> MCPServerConfig:
            self.o.log.append("config_for")
            self.o.args["config_for"] = (entry_point, environment)
            return config()

        async def start(self, cfg: MCPServerConfig) -> str | None:
            self.o.log.append("start")
            return self.o.start_reason

        def restrict_tools(self, manifest: Any, server: str, permissions: Any) -> None:
            self.o.log.append(f"restrict_tools {server}")

        async def apply_hints(self, manifest: Any, server: str, permissions: Any) -> list[str]:
            self.o.log.append(f"apply_hints {server}")
            return self.o.hints

        def bind(self, extension_id: str, server: str) -> None:
            self.o.log.append(f"bind {extension_id} {server}")

        def unbind(self, extension_id: str) -> None:
            self.o.log.append("unbind")

        async def disconnect(self, extension_id: str) -> None:
            self.o.log.append("disconnect")

        def forget_hints(self, extension_id: str) -> None:
            self.o.log.append("forget_hints")

    class Pages:
        def __init__(self, outer: Collaborators) -> None:
            self.o = outer

        async def serve(self, manifest: Any, bundle: Any, permissions: Any, url: Any) -> str | None:
            self.o.log.append("serve")
            self.o.args["serve"] = url
            return self.o.pages_reason

        async def drop(self, extension_id: str) -> None:
            self.o.log.append("drop")

    class Plugin:
        def __init__(self, outer: Collaborators) -> None:
            self.o = outer

        def install(self, manifest: Any, bundle: Any) -> None:
            self.o.log.append("install_plugin")

        def restrict(self, manifest: Any) -> None:
            self.o.log.append("restrict_plugin")

        def remove(self, extension_id: str) -> None:
            self.o.log.append("remove_plugin")

        def release(self, extension_id: str) -> None:
            self.o.log.append("release_audience")


@pytest.fixture
def rig(tmp_path: Path) -> Collaborators:
    return Collaborators(tmp_path)


def distributed() -> DistributionRecord:
    return DistributionRecord(
        extension_id="acme.notes", version="1.0.0", sha256="0" * 64, service_url="https://x"
    )


def prepared() -> PreparedExtension:
    return PreparedExtension(
        config=config(), environment=ProcessEnvironment(), permissions=Permissions(load())
    )


class TestPreparing:
    async def test_assets_then_package_then_service_then_environment_then_config_then_sandbox(
        self, rig: Collaborators, tmp_path: Path
    ) -> None:
        extension = load()

        result = await rig.runtime().prepare(
            extension, tmp_path, Permissions(extension), distributed()
        )

        assert rig.log == [
            "lay_out_assets",
            "python_entry",
            "service_url",
            "build",
            "config_for",
            "confine",
        ]
        assert isinstance(result, PreparedExtension)

    async def test_each_step_is_given_what_the_one_before_made(
        self, rig: Collaborators, tmp_path: Path
    ) -> None:
        extension = load()

        result = await rig.runtime().prepare(
            extension, tmp_path, Permissions(extension), distributed()
        )

        assert rig.args["build"] == ("https://notes.example.com", tmp_path / "assets")
        entry, environment = rig.args["config_for"]
        assert entry == tmp_path / "bin" / "acme-notes"
        assert environment.data_dir == tmp_path / "data"
        cfg, kwargs = rig.args["confine"]
        assert cfg.command == "acme-notes"
        assert kwargs == {
            "bundle": tmp_path,
            "data_dir": tmp_path / "data",
            "service_url": "https://notes.example.com",
        }
        # What is started is what the sandbox returned, not what was configured.
        assert result.config.command == "confined"
        assert result.service_url == "https://notes.example.com"
        assert result.environment.data_dir == tmp_path / "data"

    async def test_a_refusal_part_way_stops_the_steps_after_it(
        self, rig: Collaborators, tmp_path: Path
    ) -> None:
        rig.fail_assets = AssetError("asset model is not on this machine")
        extension = load()

        with pytest.raises(AssetError):
            await rig.runtime().prepare(extension, tmp_path, Permissions(extension), None)

        assert rig.log == ["lay_out_assets"]

    async def test_an_install_error_from_the_packaging_reaches_the_caller(
        self, rig: Collaborators, tmp_path: Path
    ) -> None:
        rig.fail_assets = InstallError("cannot be honoured here")
        extension = load()

        with pytest.raises(InstallError, match="cannot be honoured"):
            await rig.runtime().prepare(extension, tmp_path, Permissions(extension), None)

    def test_the_permissions_come_from_the_environment(self, rig: Collaborators) -> None:
        permissions = rig.runtime().permissions(load())

        assert isinstance(permissions, Permissions)
        assert rig.log == ["permissions"]


class TestStarting:
    async def test_a_started_service_has_its_tools_restricted_and_its_hints_read(
        self, rig: Collaborators
    ) -> None:
        rig.hints = ["tool x says it is read-only and the manifest does not"]

        result = await rig.runtime().start(load(), prepared())

        assert result == ServiceStart(
            reason=None, problems=["tool x says it is read-only and the manifest does not"]
        )
        assert rig.log == ["start", "restrict_tools ext:acme.notes", "apply_hints ext:acme.notes"]

    async def test_a_service_that_will_not_start_has_no_tools_to_restrict_and_says_why(
        self, rig: Collaborators
    ) -> None:
        rig.start_reason = "its service did not start: command not found"

        result = await rig.runtime().start(load(), prepared())

        assert result == ServiceStart(reason="its service did not start: command not found")
        assert rig.log == ["start"]


class TestPublishing:
    async def test_the_calls_are_bound_before_the_pages_and_the_plugin_follows(
        self, rig: Collaborators, tmp_path: Path
    ) -> None:
        reason = await rig.runtime().publish(load(), tmp_path, prepared())

        assert reason is None
        assert rig.log == [
            "bind acme.notes ext:acme.notes",
            "serve",
            "install_plugin",
            "restrict_plugin",
        ]

    async def test_the_pages_are_given_the_service_the_extension_belongs_to(
        self, rig: Collaborators, tmp_path: Path
    ) -> None:
        extension_prepared = prepared().model_copy(update={"service_url": "https://svc.example"})

        await rig.runtime().publish(load(), tmp_path, extension_prepared)

        assert rig.args["serve"] == "https://svc.example"

    async def test_pages_that_cannot_be_served_unbind_the_calls_again(
        self, rig: Collaborators, tmp_path: Path
    ) -> None:
        rig.pages_reason = "its UI could not be served: no free port"

        reason = await rig.runtime().publish(load(), tmp_path, prepared())

        assert reason == "its UI could not be served: no free port"
        assert rig.log == ["bind acme.notes ext:acme.notes", "serve", "unbind"]


class TestLettingGo:
    async def test_abandoning_takes_back_the_tokens_and_lets_the_sandbox_go(
        self, rig: Collaborators
    ) -> None:
        await rig.runtime().abandon("acme.notes")

        assert rig.log == ["revoke", "release"]

    async def test_stopping_unbinds_before_it_disconnects_and_forgets_last(
        self, rig: Collaborators
    ) -> None:
        await rig.runtime().stop("acme.notes")

        assert rig.log == [
            "unbind",
            "drop",
            "disconnect",
            "remove_plugin",
            "release_audience",
            "forget_hints",
            "revoke",
            "release",
        ]

    async def test_stopping_what_was_never_started_is_harmless(self, rig: Collaborators) -> None:
        runtime = rig.runtime()

        await runtime.stop("acme.notes")
        await runtime.stop("acme.notes")

        assert rig.log.count("unbind") == 2

    def test_the_confinement_is_what_the_process_reports(self, rig: Collaborators) -> None:
        status = rig.runtime().confinement("acme.notes")

        assert status == Confinement(extension_id="acme.notes", reason="no sandbox here")
