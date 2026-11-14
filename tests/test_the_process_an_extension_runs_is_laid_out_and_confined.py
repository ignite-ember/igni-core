"""The process igni starts for an extension: its assets, its package, its sandbox.

A distributed extension's assets come from igni and nowhere else; a
development install fetches a missing one from its manifest's source,
held to the digest. ``python`` packaging needs somewhere to be
installed, and a development install alone may be built from the bundle.
The sandbox is handed everything it decides on. Tried here on the
process on its own, with the sandbox and the environments played by
recorders and the asset source by a mock transport.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from igni_core.core.mcp.config import MCPServerConfig, MCPTransport
from igni_core.extensions.assets import AssetError, AssetPlatform, AssetSource
from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.install_process import ExtensionProcess
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.provenance import DistributionRecord
from igni_core.extensions.sandbox import Confinement
from igni_core.extensions.store import ExtensionStore
from tests.extension_distribution_helpers import asset
from tests.extension_distribution_helpers import manifest as raw_manifest

HERE = AssetPlatform.current()
MODEL = b"the same model everywhere"


def load(**changes: Any) -> ExtensionManifest:
    return ExtensionManifest.model_validate(raw_manifest(**changes))


def with_model(body: bytes = MODEL) -> ExtensionManifest:
    return load(assets=[asset("model", "any", body, file="model.onnx")])


def distributed() -> DistributionRecord:
    return DistributionRecord(
        extension_id="acme.notes",
        version="1.0.0",
        sha256="0" * 64,
        service_url="https://notes.example.com",
    )


class Sandbox:
    """What every process runs inside, recording what it was asked."""

    def __init__(self) -> None:
        self.confined: list[dict[str, Any]] = []
        self.released: list[str] = []

    async def confine(
        self, extension: ExtensionManifest, config: MCPServerConfig, **kwargs: Any
    ) -> MCPServerConfig:
        self.confined.append({"manifest": extension, "config": config, **kwargs})
        return config.model_copy(update={"command": "sandbox-exec"})

    async def release(self, extension_id: str) -> None:
        self.released.append(extension_id)

    def status(self, extension_id: str) -> Confinement | None:
        return Confinement(extension_id=extension_id, reason="no sandbox here")


class Environments:
    def __init__(self) -> None:
        self.ensured: list[dict[str, Any]] = []

    async def ensure(
        self, extension: ExtensionManifest, bundle: Path, *, development: bool
    ) -> Path:
        self.ensured.append({"bundle": bundle, "development": development})
        return bundle / "bin" / "acme-notes"


class Downloads:
    """The place a development install fetches an asset from."""

    def __init__(self, body: bytes = MODEL, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[str] = []
        self.threads: list[str] = []

    def source(self) -> AssetSource:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            self.threads.append(threading.current_thread().name)
            return httpx.Response(self.status, content=self.body)

        return AssetSource(transport=httpx.MockTransport(handle))


def process(
    tmp_path: Path,
    *,
    downloads: Downloads | None = None,
    sandbox: Sandbox | None = None,
    environments: Environments | None = None,
    **context: Any,
) -> ExtensionProcess:
    return ExtensionProcess(
        context=InstallContext(**context),
        asset_source=(downloads or Downloads()).source(),
        environments=environments,  # type: ignore[arg-type]
        store=ExtensionStore(tmp_path / "store"),
        sandbox=sandbox,  # type: ignore[arg-type]
    )


def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "bundle"
    path.mkdir()
    return path


def hold(tmp_path: Path, body: bytes) -> None:
    """Put an asset in the machine's cache, as an earlier sync would have."""
    cache = ExtensionStore(tmp_path / "store").asset_cache
    partial = cache.partial()
    partial.write_bytes(body)
    cache.adopt(partial, with_model(body).assets[0])


pytestmark_assets = pytest.mark.skipif(HERE is None, reason="igni ships nothing for this platform")


class TestLayingOutAssets:
    async def test_an_extension_with_no_assets_has_no_directory(self, tmp_path: Path) -> None:
        downloads = Downloads()

        laid = await process(tmp_path, downloads=downloads).lay_out_assets(
            load(), bundle(tmp_path), None
        )

        assert laid is None
        assert downloads.requests == []
        assert not (tmp_path / "bundle" / "assets").exists()

    @pytestmark_assets
    async def test_a_machine_igni_ships_nothing_for_is_refused_naming_the_platforms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(AssetPlatform, "current", classmethod(lambda cls: None))
        extension = load(
            assets=[
                asset("jre", str(platform), b"x", file="jre.bin")
                for platform in AssetPlatform.specific()
            ]
        )

        with pytest.raises(InstallError, match="this machine is none of them") as caught:
            await process(tmp_path).lay_out_assets(extension, bundle(tmp_path), None)

        assert "linux-x86_64, macos-arm64, windows-x86_64" in str(caught.value)

    @pytestmark_assets
    async def test_a_distributed_extension_missing_an_asset_is_refused_and_never_fetches_it(
        self, tmp_path: Path
    ) -> None:
        downloads = Downloads()

        with pytest.raises(InstallError, match="the next sync with igni server downloads it"):
            await process(tmp_path, downloads=downloads).lay_out_assets(
                with_model(), bundle(tmp_path), distributed()
            )

        assert downloads.requests == []

    @pytestmark_assets
    async def test_a_distributed_extension_holding_its_assets_has_them_laid_out(
        self, tmp_path: Path
    ) -> None:
        hold(tmp_path, MODEL)

        laid = await process(tmp_path).lay_out_assets(with_model(), bundle(tmp_path), distributed())

        assert laid == tmp_path / "bundle" / "assets"
        assert (laid / "model" / "model.onnx").read_bytes() == MODEL

    @pytestmark_assets
    async def test_a_development_install_fetches_what_is_missing_off_the_event_loop(
        self, tmp_path: Path
    ) -> None:
        downloads = Downloads()

        laid = await process(tmp_path, downloads=downloads).lay_out_assets(
            with_model(), bundle(tmp_path), None
        )

        assert downloads.requests == ["https://downloads.example/model/any"]
        assert downloads.threads and threading.main_thread().name not in downloads.threads
        assert laid is not None and (laid / "model" / "model.onnx").read_bytes() == MODEL

    @pytestmark_assets
    async def test_a_development_install_does_not_fetch_what_the_machine_holds(
        self, tmp_path: Path
    ) -> None:
        hold(tmp_path, MODEL)
        downloads = Downloads()

        await process(tmp_path, downloads=downloads).lay_out_assets(
            with_model(), bundle(tmp_path), None
        )

        assert downloads.requests == []

    @pytestmark_assets
    async def test_what_is_fetched_must_be_the_bytes_the_manifest_pins(
        self, tmp_path: Path
    ) -> None:
        downloads = Downloads(body=b"the same model evarywhere")

        with pytest.raises(AssetError):
            await process(tmp_path, downloads=downloads).lay_out_assets(
                with_model(), bundle(tmp_path), None
            )

        assert not (tmp_path / "bundle" / "assets" / "model").exists()

    @pytestmark_assets
    async def test_a_source_that_does_not_answer_is_an_asset_error(self, tmp_path: Path) -> None:
        with pytest.raises(AssetError, match="answered 503"):
            await process(tmp_path, downloads=Downloads(status=503)).lay_out_assets(
                with_model(), bundle(tmp_path), None
            )

    @pytestmark_assets
    async def test_only_this_platform_s_assets_are_wanted(self, tmp_path: Path) -> None:
        mine = b"a jre for here"
        theirs = b"a jre for somewhere else"
        extension = load(
            assets=[
                asset("jre", str(p), mine if p is HERE else theirs, file="jre.bin")
                for p in AssetPlatform.specific()
            ]
        )
        downloads = Downloads(body=mine)

        laid = await process(tmp_path, downloads=downloads).lay_out_assets(
            extension, bundle(tmp_path), None
        )

        assert len(downloads.requests) == 1
        assert downloads.requests[0].endswith(f"/jre/{HERE}")
        assert laid is not None and (laid / "jre" / "jre.bin").read_bytes() == mine


class TestThePackage:
    async def test_a_manifest_that_is_not_a_python_package_has_no_console_script(
        self, tmp_path: Path
    ) -> None:
        environments = Environments()
        extension = ExtensionManifest.model_validate(
            {
                "id": "acme.notes",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["python", "server.py"]},
            }
        )

        entry = await process(tmp_path, environments=environments).python_entry(
            extension, bundle(tmp_path), None
        )

        assert entry is None
        assert environments.ensured == []

    async def test_a_python_package_with_nowhere_to_be_installed_cannot_start_here(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(InstallError, match="nowhere to install one"):
            await process(tmp_path).python_entry(load(), bundle(tmp_path), distributed())

    async def test_a_distributed_package_is_never_built_from_the_bundle(
        self, tmp_path: Path
    ) -> None:
        environments = Environments()
        path = bundle(tmp_path)

        entry = await process(tmp_path, environments=environments).python_entry(
            load(), path, distributed()
        )

        assert entry == path / "bin" / "acme-notes"
        assert environments.ensured == [{"bundle": path, "development": False}]

    async def test_a_development_install_may_build_it_from_the_bundle(self, tmp_path: Path) -> None:
        environments = Environments()

        await process(tmp_path, environments=environments).python_entry(
            load(), bundle(tmp_path), None
        )

        assert environments.ensured[0]["development"] is True


class TestTheSandbox:
    @pytest.mark.parametrize("development", [False, True])
    async def test_it_is_handed_everything_it_decides_on(
        self, tmp_path: Path, development: bool
    ) -> None:
        sandbox = Sandbox()
        config = MCPServerConfig(
            name="ext:acme.notes", type=MCPTransport.stdio, command="acme-notes", args=["--x"]
        )
        project = tmp_path / "project"
        extension = load()
        path = bundle(tmp_path)

        confined = await process(
            tmp_path, sandbox=sandbox, project_dir=project, allow_development=development
        ).confine(
            extension,
            config,
            bundle=path,
            data_dir=tmp_path / "data",
            service_url="https://notes.example.com",
        )

        assert confined.command == "sandbox-exec"  # what the sandbox returned, not the input
        [call] = sandbox.confined
        assert call["manifest"] is extension
        assert call["config"] is config
        assert call["bundle"] == path
        assert call["data_dir"] == tmp_path / "data"
        assert call["project_dir"] == project
        assert call["service_url"] == "https://notes.example.com"
        assert call["development"] is development

    async def test_releasing_and_asking_for_the_status_reach_the_sandbox(
        self, tmp_path: Path
    ) -> None:
        sandbox = Sandbox()
        running = process(tmp_path, sandbox=sandbox)

        await running.release("acme.notes")
        status = running.status("acme.notes")

        assert sandbox.released == ["acme.notes"]
        assert status == Confinement(extension_id="acme.notes", reason="no sandbox here")

    def test_with_no_sandbox_given_an_unknown_extension_has_no_confinement_to_report(
        self, tmp_path: Path
    ) -> None:
        assert process(tmp_path).status("never.started") is None
