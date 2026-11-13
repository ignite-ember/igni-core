"""An extension introduced through igni server has a home on the web.

Its service's URL — the distribution record's ``service_url`` — is the
service's ``client_uri``, which RFC 7591 defines as the client's home
page. igni shows it as "Open on the web" and the host object model
reports it as ``web_url``. A development install uses its ``serviceUrl``
setting when one is set; with neither, there is no link.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from igni_core.backend.rpc_handlers.base import RpcHandlerContext
from igni_core.backend.rpc_handlers.extensions import ExtensionsRpcHandler
from igni_core.extensions.provenance import DistributionRecord
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore
from igni_core.extensions.web_home import ExtensionWebHome
from tests.host_model_helpers import manifest


def record(extension_id: str, service_url: str) -> DistributionRecord:
    return DistributionRecord(
        extension_id=extension_id, version="1.0.0", sha256="0" * 64, service_url=service_url
    )


def test_a_distributed_extension_is_at_its_service(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    store.provenance.record(record("acme.reviewer", "https://reviewer.acme.test"))
    assert (
        ExtensionWebHome(store).url_for(manifest("acme.reviewer")) == "https://reviewer.acme.test"
    )


def test_the_distribution_record_wins_over_a_setting(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    m = manifest("acme.reviewer")
    store.save_settings(m, {"serviceUrl": "https://dev.acme.test"})
    store.provenance.record(record("acme.reviewer", "https://reviewer.acme.test"))
    assert ExtensionWebHome(store).url_for(m) == "https://reviewer.acme.test"


def test_a_development_install_uses_its_service_url_setting_or_has_none(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    m = manifest("acme.dev")
    home = ExtensionWebHome(store)
    assert home.url_for(m) is None
    store.save_settings(m, {"serviceUrl": "  https://dev.acme.test  "})
    assert home.url_for(m) == "https://dev.acme.test"


def test_only_a_web_address_is_a_home_page(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    m = manifest("acme.dev")
    store.save_settings(m, {"serviceUrl": "javascript:alert(1)"})
    assert ExtensionWebHome(store).url_for(m) is None


def test_a_record_with_no_service_url_falls_back_to_the_setting(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    m = manifest("acme.reviewer")
    store.save_settings(m, {"serviceUrl": "https://dev.acme.test"})
    store.provenance.record(record("acme.reviewer", ""))
    assert ExtensionWebHome(store).url_for(m) == "https://dev.acme.test"


def test_a_record_that_is_not_a_web_address_is_no_home_and_does_not_fall_back(
    tmp_path: Path,
) -> None:
    """The record is what igni verified; a setting must not stand in for it."""
    store = ExtensionStore(tmp_path)
    m = manifest("acme.reviewer")
    store.save_settings(m, {"serviceUrl": "https://dev.acme.test"})
    store.provenance.record(record("acme.reviewer", "javascript:alert(1)"))
    assert ExtensionWebHome(store).url_for(m) is None


def test_the_record_s_address_is_trimmed_and_http_is_a_web_address(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    store.provenance.record(record("acme.reviewer", "  http://reviewer.acme.test/home  "))
    assert (
        ExtensionWebHome(store).url_for(manifest("acme.reviewer"))
        == "http://reviewer.acme.test/home"
    )


@pytest.mark.parametrize(
    "setting", ["", "   ", "/relative/path", "ftp://x.test", "data:text/html,x", "file:///etc", "5"]
)
def test_a_setting_that_is_not_a_web_address_is_no_home(tmp_path: Path, setting: str) -> None:
    store = ExtensionStore(tmp_path)
    m = manifest("acme.dev")
    store.save_settings(m, {"serviceUrl": setting})
    assert ExtensionWebHome(store).url_for(m) is None


@pytest.mark.parametrize("scheme", ["HTTPS", "Https", "HTTP"])
def test_a_scheme_is_a_web_scheme_in_any_case(tmp_path: Path, scheme: str) -> None:
    store = ExtensionStore(tmp_path)
    m = manifest("acme.dev")
    store.save_settings(m, {"serviceUrl": f"{scheme}://dev.acme.test"})
    assert ExtensionWebHome(store).url_for(m) == f"{scheme}://dev.acme.test"


async def test_the_extension_list_carries_it_for_the_window(tmp_path: Path) -> None:
    """``list_extensions`` is what the Extensions panel and pages draw from."""
    registry = ExtensionRegistry()
    registry.install(manifest("acme.reviewer", host=["chat.read"]), state=ExtensionState.READY)
    registry.install(manifest("acme.plain"), state=ExtensionState.READY)
    store = ExtensionStore(tmp_path)
    store.provenance.record(record("acme.reviewer", "https://reviewer.acme.test"))
    ctx = RpcHandlerContext(
        backend=None,
        file_completion=MagicMock(),
        shell_runner=MagicMock(),
        login=None,
        push=None,
        workflows=None,  # type: ignore[arg-type]
        extensions=ExtensionRpc(registry),  # type: ignore[arg-type]
        extension_store=store,
    )

    listed = await ExtensionsRpcHandler(ctx).list_extensions({})

    by_id = {e.id: json.loads(e.model_dump_json()) for e in listed.extensions}
    assert by_id["acme.reviewer"]["web_url"] == "https://reviewer.acme.test"
    assert by_id["acme.reviewer"]["host"] == ["chat.read"]
    assert by_id["acme.plain"]["web_url"] is None
