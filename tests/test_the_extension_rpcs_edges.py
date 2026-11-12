"""The edges of the extension RPCs: what the window is told when the
backend knows less, and what a settings write refuses.

An extension is listed with the address of its UI, its web home and its
service; a backend with no origins (nothing serves a UI) leaves the UI
and service addresses empty but still lists it. An extension that
vanishes between the registry's list of ids and its lookup is skipped,
not a crash. A setting's value must be one of its options, an empty
write changes nothing, and a refused write leaves what was stored.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from igni_core.backend.rpc_handlers.base import RpcHandlerContext
from igni_core.backend.rpc_handlers.extensions import ExtensionsRpcHandler
from igni_core.backend.schemas_extensions import SetExtensionSettingsArgs
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.provenance import DistributionRecord
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore


def _manifest(extension_id: str) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": extension_id,
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
            "contributes": {
                "settings": [
                    {
                        "key": "failOn",
                        "type": "enum",
                        "label": "Fail on",
                        "options": ["low", "high"],
                    },
                    {"key": "note", "type": "string", "label": "Note"},
                ]
            },
        }
    )


def _handler(
    registry: ExtensionRegistry, store: ExtensionStore, origins: object | None = None
) -> ExtensionsRpcHandler:
    return ExtensionsRpcHandler(
        RpcHandlerContext(
            backend=None,  # type: ignore[arg-type]
            file_completion=MagicMock(),
            shell_runner=MagicMock(),
            login=None,  # type: ignore[arg-type]
            push=None,  # type: ignore[arg-type]
            workflows=None,  # type: ignore[arg-type]
            extensions=ExtensionRpc(registry),  # type: ignore[arg-type]
            origins=origins,  # type: ignore[arg-type]
            extension_store=store,
        )
    )


def _installed(tmp_path: Path, *ids: str) -> tuple[ExtensionRegistry, ExtensionStore]:
    registry = ExtensionRegistry()
    for extension_id in ids:
        registry.install(_manifest(extension_id), state=ExtensionState.READY)
    return registry, ExtensionStore(tmp_path)


async def test_without_origins_the_ui_and_service_addresses_are_empty(tmp_path: Path) -> None:
    registry, store = _installed(tmp_path, "acme.scanner")

    listed = await _handler(registry, store, origins=None).list_extensions({})

    (summary,) = listed.extensions
    assert summary.id == "acme.scanner"
    assert summary.origin == ""
    assert summary.service_url is None


async def test_the_ui_the_home_and_the_service_each_get_their_own_address(tmp_path: Path) -> None:
    registry, store = _installed(tmp_path, "acme.scanner")
    store.provenance.record(
        DistributionRecord(
            extension_id="acme.scanner",
            version="1.0.0",
            sha256="0" * 64,
            service_url="https://scanner.acme.test",
        )
    )
    origins = SimpleNamespace(
        url_for=lambda extension_id: f"http://127.0.0.1:9000/{extension_id}",
        service_url_for=lambda extension_id: f"https://service.acme.test/{extension_id}",
    )

    listed = await _handler(registry, store, origins).list_extensions({})

    (summary,) = listed.extensions
    assert summary.origin == "http://127.0.0.1:9000/acme.scanner"
    assert summary.web_url == "https://scanner.acme.test"
    assert summary.service_url == "https://service.acme.test/acme.scanner"


async def test_an_extension_that_vanished_between_the_ids_and_the_lookup_is_skipped(
    tmp_path: Path,
) -> None:
    class Vanishing(ExtensionRegistry):
        def get(self, extension_id: str):  # type: ignore[no-untyped-def]
            return None if extension_id == "acme.gone" else super().get(extension_id)

    registry = Vanishing()
    for extension_id in ("acme.here", "acme.gone", "acme.also"):
        registry.install(_manifest(extension_id), state=ExtensionState.READY)

    listed = await _handler(registry, ExtensionStore(tmp_path)).list_extensions({})

    assert sorted(e.id for e in listed.extensions) == ["acme.also", "acme.here"]


async def test_a_value_outside_a_settings_options_is_refused_and_not_stored(
    tmp_path: Path,
) -> None:
    registry, store = _installed(tmp_path, "acme.scanner")
    handler = _handler(registry, store)

    with pytest.raises(ValueError, match="failOn"):
        await handler.set_extension_settings(
            SetExtensionSettingsArgs(extension_id="acme.scanner", values={"failOn": "medium"})
        )

    (summary,) = (await handler.list_extensions({})).extensions
    assert "failOn" not in summary.values


async def test_a_value_among_the_options_is_stored(tmp_path: Path) -> None:
    registry, store = _installed(tmp_path, "acme.scanner")

    result = await _handler(registry, store).set_extension_settings(
        SetExtensionSettingsArgs(extension_id="acme.scanner", values={"failOn": "high"})
    )

    assert result.extensions[0].values["failOn"] == "high"


async def test_saving_nothing_changes_nothing_and_answers_the_list(tmp_path: Path) -> None:
    registry, store = _installed(tmp_path, "acme.scanner")
    handler = _handler(registry, store)
    await handler.set_extension_settings(
        SetExtensionSettingsArgs(extension_id="acme.scanner", values={"note": "keep"})
    )

    result = await handler.set_extension_settings(
        SetExtensionSettingsArgs(extension_id="acme.scanner", values={})
    )

    assert [e.id for e in result.extensions] == ["acme.scanner"]
    assert result.extensions[0].values["note"] == "keep"


async def test_a_write_merges_over_what_is_stored(tmp_path: Path) -> None:
    registry, store = _installed(tmp_path, "acme.scanner")
    handler = _handler(registry, store)
    await handler.set_extension_settings(
        SetExtensionSettingsArgs(extension_id="acme.scanner", values={"note": "keep"})
    )

    result = await handler.set_extension_settings(
        SetExtensionSettingsArgs(extension_id="acme.scanner", values={"failOn": "low"})
    )

    assert result.extensions[0].values == {"note": "keep", "failOn": "low"}


async def test_with_a_registry_but_no_store_nothing_is_listed_or_saved() -> None:
    handler = ExtensionsRpcHandler(
        RpcHandlerContext(
            backend=None,  # type: ignore[arg-type]
            file_completion=MagicMock(),
            shell_runner=MagicMock(),
            login=None,  # type: ignore[arg-type]
            push=None,  # type: ignore[arg-type]
            workflows=None,  # type: ignore[arg-type]
            extensions=ExtensionRpc(ExtensionRegistry()),  # type: ignore[arg-type]
        )
    )

    assert (await handler.list_extensions({})).extensions == []
    with pytest.raises(ValueError, match="no extension support"):
        await handler.set_extension_settings(
            SetExtensionSettingsArgs(extension_id="acme.scanner", values={})
        )
