"""``ExtensionDirectory``: the installed extensions as the host model tells them.

Grants, info and settings of the extensions in the registry, with the
refusal a request about one that is not installed gets, and the save that
goes through the same RPC the Extensions page uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.extension_directory import ExtensionDirectory
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.wire import ErrorBody
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.store import ExtensionStore
from igni_core.extensions.web_home import ExtensionWebHome
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import manifest


class Gateway:
    """The backend gateway: ``set_extension_settings`` saves into ``store``."""

    def __init__(self, registry: ExtensionRegistry, store: ExtensionStore) -> None:
        self.registry = registry
        self.store = store
        self.asked: list[tuple[RpcMethod, dict[str, Any], str]] = []
        self.failure: Exception | None = None

    def default_session(self) -> str:
        return "boot"

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        self.asked.append((method, args, session_id))
        if self.failure is not None:
            raise self.failure
        entry = self.registry.get(args["extension_id"])
        assert entry is not None
        self.store.save_settings(entry.manifest, args["values"])


class KeptSettingsStore(ExtensionStore):
    """A store that hands out the one dict it keeps, as a cache would."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.kept: dict[str, object] = {"serviceUrl": "https://kept.example.com/"}

    def settings(self, manifest: Any) -> dict[str, object]:
        return self.kept


@pytest.fixture
def registry() -> ExtensionRegistry:
    return ExtensionRegistry()


@pytest.fixture
def gateway(registry: ExtensionRegistry, tmp_path: Path) -> Gateway:
    return Gateway(registry, ExtensionStore(tmp_path / "extensions"))


@pytest.fixture
def directory(registry: ExtensionRegistry, gateway: Gateway) -> ExtensionDirectory:
    return ExtensionDirectory(
        registry,
        gateway.store,
        ExtensionWebHome(gateway.store),
        gateway,  # type: ignore[arg-type]
    )


class TestGrants:
    def test_an_installed_extension_has_the_grants_of_its_manifest(
        self, registry: ExtensionRegistry, directory: ExtensionDirectory
    ) -> None:
        registry.install(manifest("acme.one", host=["links"]))
        grants = directory.grants_for("acme.one")
        assert grants is not None and grants.extension_id == "acme.one"
        assert grants.allows("links") and not grants.allows("chat.read")

    def test_one_that_is_not_installed_has_none(self, directory: ExtensionDirectory) -> None:
        assert directory.grants_for("acme.gone") is None

    def test_a_request_from_one_that_is_not_installed_gets_the_not_found_body(
        self, directory: ExtensionDirectory
    ) -> None:
        refusal = directory.require_grants("acme.gone")
        assert refusal == ErrorBody(
            code=ErrorCode.NOT_FOUND, message="no extension 'acme.gone' is installed"
        )

    def test_one_that_is_installed_gets_its_grants(
        self, registry: ExtensionRegistry, directory: ExtensionDirectory
    ) -> None:
        registry.install(manifest("acme.one"))
        grants = directory.require_grants("acme.one")
        assert not isinstance(grants, ErrorBody) and grants.extension_id == "acme.one"


class TestInfo:
    def test_info_tells_state_reason_and_the_web_home_from_settings(
        self, registry: ExtensionRegistry, directory: ExtensionDirectory, gateway: Gateway
    ) -> None:
        entry = registry.install(manifest("acme.one"), state=ExtensionState.FAILED, reason="boom")
        gateway.store.save_settings(entry.manifest, {"serviceUrl": "https://acme.example.com/app"})
        info = directory.info("acme.one")
        assert (info.id, info.version, info.state, info.reason) == (
            "acme.one",
            "1.0.0",
            "FAILED",
            "boom",
        )
        assert info.web_url == "https://acme.example.com/app"

    def test_without_a_service_url_there_is_no_web_home(
        self, registry: ExtensionRegistry, directory: ExtensionDirectory
    ) -> None:
        registry.install(manifest("acme.one"))
        assert directory.info("acme.one").web_url is None

    def test_info_of_one_that_is_not_installed_is_refused_not_found(
        self, directory: ExtensionDirectory
    ) -> None:
        with pytest.raises(HostModelError) as caught:
            directory.info("acme.gone")
        assert caught.value.code is ErrorCode.NOT_FOUND
        assert caught.value.message == "no extension 'acme.gone' is installed"

    def test_installed_lists_every_extension_by_id(
        self, registry: ExtensionRegistry, directory: ExtensionDirectory
    ) -> None:
        registry.install(manifest("acme.b"))
        registry.install(manifest("acme.a"))
        assert [i.id for i in directory.installed()] == ["acme.a", "acme.b"]
        assert directory.installed()[0].state == "READY"

    def test_nothing_installed_lists_nothing(self, directory: ExtensionDirectory) -> None:
        assert directory.installed() == []

    def test_an_entry_that_vanishes_between_ids_and_get_is_skipped(
        self, registry: ExtensionRegistry, directory: ExtensionDirectory
    ) -> None:
        registry.install(manifest("acme.a"))
        registry.install(manifest("acme.b"))
        real_ids = registry.ids

        def ids_then_remove() -> list[str]:
            found = real_ids()
            registry.remove("acme.a")
            return found

        registry.ids = ids_then_remove  # type: ignore[method-assign]
        assert [i.id for i in directory.installed()] == ["acme.b"]


class TestSettings:
    def test_settings_are_the_saved_values_with_defaults_and_a_copy(
        self, registry: ExtensionRegistry, directory: ExtensionDirectory, gateway: Gateway
    ) -> None:
        entry = registry.install(manifest("acme.one"))
        gateway.store.save_settings(entry.manifest, {"serviceUrl": "https://a.example.com/"})
        values = directory.settings_of("acme.one")
        assert values["serviceUrl"] == "https://a.example.com/"
        values["serviceUrl"] = "changed"
        assert directory.settings_of("acme.one")["serviceUrl"] == "https://a.example.com/"
        saved = directory.saved_settings("acme.one")
        assert saved is not None
        saved["serviceUrl"] = "changed"
        assert directory.saved_settings("acme.one") == directory.settings_of("acme.one")

    def test_settings_of_one_not_installed_is_refused_but_saved_settings_is_none(
        self, directory: ExtensionDirectory
    ) -> None:
        with pytest.raises(HostModelError) as caught:
            directory.settings_of("acme.gone")
        assert caught.value.code is ErrorCode.NOT_FOUND
        assert directory.saved_settings("acme.gone") is None

    def test_what_is_handed_out_is_a_copy_of_what_the_store_keeps(
        self, registry: ExtensionRegistry, tmp_path: Path
    ) -> None:
        store = KeptSettingsStore(tmp_path / "kept")
        gateway = Gateway(registry, store)
        directory = ExtensionDirectory(
            registry,
            store,
            ExtensionWebHome(store),
            gateway,  # type: ignore[arg-type]
        )
        registry.install(manifest("acme.one"))
        directory.settings_of("acme.one")["serviceUrl"] = "changed"
        saved = directory.saved_settings("acme.one")
        assert saved is not None
        saved["serviceUrl"] = "changed"
        assert store.kept == {"serviceUrl": "https://kept.example.com/"}

    async def test_a_save_goes_through_set_extension_settings_in_the_default_session(
        self, registry: ExtensionRegistry, directory: ExtensionDirectory, gateway: Gateway
    ) -> None:
        registry.install(manifest("acme.one"))
        change = await directory.save_settings("acme.one", {"serviceUrl": "https://b.example.com/"})
        assert gateway.asked == [
            (
                RpcMethod.SET_EXTENSION_SETTINGS,
                {"extension_id": "acme.one", "values": {"serviceUrl": "https://b.example.com/"}},
                "boot",
            )
        ]
        assert change.values["serviceUrl"] == "https://b.example.com/"

    async def test_a_refused_save_is_raised_and_nothing_is_reported_saved(
        self, registry: ExtensionRegistry, directory: ExtensionDirectory, gateway: Gateway
    ) -> None:
        registry.install(manifest("acme.one"))
        gateway.failure = HostModelError(ErrorCode.REFUSED, "not allowed")
        with pytest.raises(HostModelError, match="not allowed"):
            await directory.save_settings("acme.one", {"serviceUrl": "x"})
