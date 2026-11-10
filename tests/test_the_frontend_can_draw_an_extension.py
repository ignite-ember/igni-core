"""The client is told enough to render an installed extension.

Before this, an installed extension answered calls and had nowhere to
put them: nothing told the frontend which extensions existed, what
state each was in, or which loopback port its UI was on. The frame
component had been built and tested for months against a stub window
and was never mounted, because there was no way to know what to
mount.

``list_extensions`` is the base side of that. It is deliberately not
the manifest: a manifest holds the packaging, the egress allowlist
and the sidecar image, and none of those belong in a browser's memory
or in anything that logs a WebSocket frame.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from igni_core.backend.rpc_handlers.base import RpcHandlerContext
from igni_core.backend.rpc_handlers.extensions import ExtensionsRpcHandler
from igni_core.backend.schemas_extensions import SetExtensionSettingsArgs
from igni_core.core.mcp.client import MCPClientManager
from igni_core.extensions.installer import ExtensionInstaller
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def bundle_at(tmp_path: Path) -> Path:
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["dev"]["command"] = [sys.executable, "server.py"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def handler(
    extensions: ExtensionRpc | None,
    origins: ExtensionOrigins | None,
    store: ExtensionStore | None = None,
):
    ctx = RpcHandlerContext(
        backend=None,
        file_completion=MagicMock(),
        shell_runner=MagicMock(),
        login=None,  # type: ignore[arg-type]
        push=None,  # type: ignore[arg-type]
        workflows=None,  # type: ignore[arg-type]
        extensions=extensions,
        origins=origins,
        extension_store=store,
    )
    return ExtensionsRpcHandler(ctx)


class Wired:
    def __init__(self, tmp_path: Path) -> None:
        self.registry = ExtensionRegistry()
        self.rpc = ExtensionRpc(self.registry)
        self.mcp = MCPClientManager(project_dir=tmp_path)
        self.origins = ExtensionOrigins()
        self.store = ExtensionStore(tmp_path / "store")
        self.installer = ExtensionInstaller(
            registry=self.registry,
            rpc=self.rpc,
            mcp=self.mcp,
            origins=self.origins,
            allow_development=True,
        )

    async def close(self) -> None:
        await self.origins.stop_all()
        await self.mcp.disconnect_all()


@pytest.fixture
async def wired(tmp_path: Path):
    w = Wired(tmp_path)
    try:
        yield w
    finally:
        await w.close()


async def test_an_installed_extension_is_listed_with_what_it_takes_to_draw(wired, tmp_path):
    await wired.installer.install(bundle_at(tmp_path))
    result = await handler(wired.rpc, wired.origins, wired.store).list_extensions({})

    (summary,) = result.extensions
    assert summary.id == "acme.scanner"
    assert summary.name == "Compliance Scanner (dev)"
    assert summary.state == ExtensionState.READY.value
    assert summary.reason == ""
    assert summary.origin.startswith("http://127.0.0.1:")
    assert summary.allow == ["ext.acme.scanner.*"]
    assert [p.kind for p in summary.pages] == ["acme.scan"]
    assert summary.pages[0].sidebar is True
    assert summary.pages[0].entry == "index.html"


async def test_the_manifest_itself_does_not_go_to_the_browser(wired, tmp_path):
    """Only what is needed to draw.

    The packaging, the egress allowlist and the sidecar image are
    none of a frontend's business, and sending them would put each of
    them in anything that logs a frame.
    """
    await wired.installer.install(bundle_at(tmp_path))
    result = await handler(wired.rpc, wired.origins, wired.store).list_extensions({})

    fields = set(result.extensions[0].model_dump())
    assert fields == {
        "id",
        "name",
        "version",
        "state",
        "reason",
        "origin",
        "allow",
        "pages",
        "menu",
        # Status chips in the host's own chrome. The host renders
        # them; the extension decides what they say.
        "indicators",
        # The settings *shape*, so the host can render a form, and
        # the configured *values*, which become the frame's launch
        # parameters. Neither is sensitive by contract: a secret is
        # declared in `permissions.secrets` and resolved by the
        # deployment, never typed into a setting — `Setting.type` has
        # no password among its four.
        "settings",
        "values",
        # The host-model objects it was granted, for display — the
        # backend checks every request against the manifest itself.
        "host",
        # Its home on the web, the service's `client_uri`, for "Open
        # on the web". Public by nature: it is the service's address.
        "web_url",
        # Its own service, as its page reaches it (`bridge.service.url`)
        # — the origin its CSP already admits. Also the service's
        # address, and no credential: the token is fetched separately.
        "service_url",
        # What a collision with another extension switched off, and
        # why: a claim's name and a sentence, nothing of the manifest.
        "switched_off",
        # Its prompt edits that land nowhere, and why: the same shape.
        "dropped",
        # What is wrong with it that does not stop it working: a
        # sentence per tool whose server and manifest disagree.
        "problems",
        # Why it is faulty — it stopped answering and igni restarted
        # it — as a sentence; empty while it answers.
        "faulty",
        # Whether its process runs in the OS sandbox, or why not — an
        # unsandboxed process has the person's own file and network
        # access, which the person should be able to see.
        "sandbox",
        # The agent's tools and what each says it is, as a sentence —
        # the hints the permission modes decide on, for the person to
        # read.
        "tools",
    }


async def test_a_broken_extension_is_listed_with_its_reason(wired, tmp_path):
    """Not filtered out.

    An administrator asking "why is Compliance missing" is better
    served by a row that says why than by an absence they have to
    interpret.
    """
    bundle = bundle_at(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["dev"]["command"] = [sys.executable, "no-such-file.py"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    await wired.installer.install(bundle)

    (summary,) = (
        await handler(wired.rpc, wired.origins, wired.store).list_extensions({})
    ).extensions
    assert summary.state == ExtensionState.FAILED.value
    assert summary.reason


async def test_a_backend_without_extension_support_answers_empty(tmp_path):
    """Truthful rather than an error: nothing is installed, and
    nothing can be."""
    result = await handler(None, None).list_extensions({})
    assert result.extensions == []


async def test_an_extension_with_no_ui_has_no_origin(wired, tmp_path):
    bundle = bundle_at(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    del manifest["contributes"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    await wired.installer.install(bundle)

    (summary,) = (
        await handler(wired.rpc, wired.origins, wired.store).list_extensions({})
    ).extensions
    assert summary.state == ExtensionState.READY.value
    assert summary.origin == ""
    assert summary.pages == []


async def test_the_listing_is_stable(wired, tmp_path):
    """Sorted by id, so the sidebar does not reorder itself between
    calls."""
    await wired.installer.install(bundle_at(tmp_path))
    first = await handler(wired.rpc, wired.origins, wired.store).list_extensions({})
    second = await handler(wired.rpc, wired.origins, wired.store).list_extensions({})
    assert [e.id for e in first.extensions] == [e.id for e in second.extensions]


async def test_settings_are_saved_and_the_whole_list_comes_back(wired, tmp_path):
    await wired.installer.install(bundle_at(tmp_path))
    result = await handler(wired.rpc, wired.origins, wired.store).set_extension_settings(
        SetExtensionSettingsArgs(extension_id="acme.scanner", values={"failOn": "low"})
    )

    (summary,) = result.extensions
    assert summary.values["failOn"] == "low"


async def test_a_setting_the_manifest_does_not_allow_is_an_rpc_error(wired, tmp_path):
    """The Extensions panel shows an error only when the call rejects."""
    await wired.installer.install(bundle_at(tmp_path))
    rpc = handler(wired.rpc, wired.origins, wired.store)
    with pytest.raises(ValueError, match="backdoor"):
        await rpc.set_extension_settings(
            SetExtensionSettingsArgs(extension_id="acme.scanner", values={"backdoor": True})
        )

    (summary,) = (await rpc.list_extensions({})).extensions
    assert "backdoor" not in summary.values


async def test_settings_for_an_extension_not_installed_are_an_rpc_error(wired):
    with pytest.raises(ValueError, match="acme.nobody"):
        await handler(wired.rpc, wired.origins, wired.store).set_extension_settings(
            SetExtensionSettingsArgs(extension_id="acme.nobody", values={})
        )


async def test_settings_without_extension_support_are_an_rpc_error():
    with pytest.raises(ValueError, match="this backend has no extension support"):
        await handler(None, None).set_extension_settings(
            SetExtensionSettingsArgs(extension_id="acme.scanner", values={})
        )
