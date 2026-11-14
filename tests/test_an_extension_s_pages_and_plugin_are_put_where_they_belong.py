"""An extension's pages get an origin of their own, and its plugin a directory of its own.

Both are optional contributions, and both fail soft: an extension whose
pages cannot be shown, or whose plugin cannot be copied, is degraded and
says so. The two classes are tried here on their own, with the origins
backend played by a recorder and the plugins directory a real one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.agents.tool_audience import ToolAudience
from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_contributions import ExtensionPages, ExtensionPlugin
from igni_core.extensions.install_service import server_name
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.permissions import Permissions
from tests.extension_distribution_helpers import manifest as raw_manifest

SERVICE = "https://notes.example.com/api"


def load(**changes: Any) -> ExtensionManifest:
    return ExtensionManifest.model_validate(raw_manifest(**changes))


PAGES = {"pages": [{"kind": "acme.notes.board", "label": "Board"}]}


class Origins:
    """What serves an extension's UI, recording what it was asked to serve."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.served: list[dict[str, Any]] = []
        self.dropped: list[str] = []

    async def serve(self, extension_id: str, root: Path, **kwargs: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.served.append({"id": extension_id, "root": root, **kwargs})

    async def drop(self, extension_id: str) -> None:
        self.dropped.append(extension_id)


def pages(origins: Origins | None, *, development: bool = False) -> ExtensionPages:
    return ExtensionPages(origins=origins, context=InstallContext(allow_development=development))  # type: ignore[arg-type]


def bundle_with_ui(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "ui").mkdir(parents=True, exist_ok=True)
    return bundle


class TestPages:
    async def test_an_extension_without_pages_serves_nothing(self, tmp_path: Path) -> None:
        origins = Origins()
        extension = load()

        reason = await pages(origins).serve(
            extension, bundle_with_ui(tmp_path), Permissions(extension), SERVICE
        )

        assert reason is None
        assert origins.served == []

    async def test_pages_with_no_origins_backend_say_no_ui_is_served(self, tmp_path: Path) -> None:
        extension = load(contributes=PAGES)

        reason = await pages(None).serve(
            extension, bundle_with_ui(tmp_path), Permissions(extension), SERVICE
        )

        assert reason is not None
        assert "acme.notes" in reason
        assert "serves no extension UI" in reason

    @pytest.mark.parametrize("make", ["missing", "file"])
    async def test_a_bundle_without_a_ui_directory_names_where_it_looked(
        self, tmp_path: Path, make: str
    ) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        if make == "file":
            (bundle / "ui").write_text("not a directory")
        origins = Origins()
        extension = load(contributes=PAGES)

        reason = await pages(origins).serve(extension, bundle, Permissions(extension), SERVICE)

        assert reason is not None
        assert f"no 'ui' directory at {bundle / 'ui'}" in reason
        assert origins.served == []

    async def test_the_ui_is_served_with_its_page_service_and_declared_egress_only(
        self, tmp_path: Path
    ) -> None:
        bundle = bundle_with_ui(tmp_path)
        origins = Origins()
        extension = load(contributes=PAGES, permissions={"egress": ["maps.example.org"]})

        reason = await pages(origins).serve(extension, bundle, Permissions(extension), SERVICE)

        assert reason is None
        [call] = origins.served
        assert call["id"] == "acme.notes"
        assert call["root"] == bundle / "ui"
        # The origin of the service, never its path; the declared host over https and wss.
        assert call["connect_src"] == (
            "'self' https://notes.example.com wss://notes.example.com "
            "https://maps.example.org wss://maps.example.org"
        )
        # The token's audience is the URL exactly as the process has it.
        assert call["service_url"] == SERVICE

    @pytest.mark.parametrize(
        "service_url",
        [None, "", "   ", "not a url", "http://notes.example.com", "ftp://notes.example.com"],
    )
    async def test_a_service_a_page_may_not_reach_is_not_recorded_or_allowed(
        self, tmp_path: Path, service_url: str | None
    ) -> None:
        origins = Origins()
        extension = load(contributes=PAGES)

        reason = await pages(origins).serve(
            extension, bundle_with_ui(tmp_path), Permissions(extension), service_url
        )

        assert reason is None
        [call] = origins.served
        assert call["connect_src"] == "'self'"
        assert call["service_url"] is None

    async def test_plain_http_to_this_machine_is_allowed_only_in_development(
        self, tmp_path: Path
    ) -> None:
        extension = load(contributes=PAGES)
        local = "http://localhost:8765/x"
        outcomes = {}
        for development in (False, True):
            origins = Origins()
            await pages(origins, development=development).serve(
                extension, bundle_with_ui(tmp_path), Permissions(extension), local
            )
            outcomes[development] = origins.served[0]

        assert outcomes[False]["service_url"] is None
        assert outcomes[True]["service_url"] == local
        assert outcomes[True]["connect_src"] == "'self' http://localhost:8765 ws://localhost:8765"

    async def test_an_origin_that_cannot_start_becomes_a_reason_not_an_exception(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        extension = load(contributes=PAGES)

        with caplog.at_level(logging.ERROR):
            reason = await pages(Origins(fail=OSError("no free port"))).serve(
                extension, bundle_with_ui(tmp_path), Permissions(extension), SERVICE
            )

        assert reason == "its UI could not be served: no free port"
        assert "serving acme.notes UI failed" in caplog.text

    async def test_dropping_stops_serving_and_is_harmless_without_a_backend(self) -> None:
        origins = Origins()

        await pages(origins).drop("acme.notes")
        await pages(None).drop("acme.notes")

        assert origins.dropped == ["acme.notes"]


def plugin_bundle(tmp_path: Path, *, name: str = "plugin", manifest_file: bool = True) -> Path:
    bundle = tmp_path / "bundle"
    plugin = bundle / name
    (plugin / "agents").mkdir(parents=True)
    (plugin / "agents" / "archivist.md").write_text("an agent")
    if manifest_file:
        (plugin / ".claude-plugin").mkdir()
        (plugin / ".claude-plugin" / "plugin.json").write_text("{}")
    return bundle


def carrying(plugin: str = "plugin", **permissions: Any) -> ExtensionManifest:
    return load(contributes={"plugin": plugin}, permissions=permissions)


class TestPlugin:
    def test_the_plugin_is_copied_under_the_extension_s_id(self, tmp_path: Path) -> None:
        plugins = tmp_path / "plugins"

        ExtensionPlugin(plugins_dir=plugins).install(carrying(), plugin_bundle(tmp_path))

        installed = plugins / "acme.notes"
        assert (installed / "agents" / "archivist.md").read_text() == "an agent"
        assert (installed / ".claude-plugin" / "plugin.json").is_file()

    def test_a_reinstall_replaces_the_previous_copy(self, tmp_path: Path) -> None:
        plugins = tmp_path / "plugins"
        (plugins / "acme.notes").mkdir(parents=True)
        (plugins / "acme.notes" / "stale.md").write_text("left from before")

        ExtensionPlugin(plugins_dir=plugins).install(carrying(), plugin_bundle(tmp_path))

        assert not (plugins / "acme.notes" / "stale.md").exists()
        assert (plugins / "acme.notes" / "agents" / "archivist.md").is_file()

    def test_no_plugin_named_installs_nothing(self, tmp_path: Path) -> None:
        plugins = tmp_path / "plugins"

        ExtensionPlugin(plugins_dir=plugins).install(load(), plugin_bundle(tmp_path))

        assert not plugins.exists()

    def test_no_plugins_directory_warns_and_installs_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            ExtensionPlugin().install(carrying(), plugin_bundle(tmp_path))

        assert "has no plugins directory configured" in caplog.text
        assert "acme.notes" in caplog.text

    @pytest.mark.parametrize("path", ["../outside", "../../etc", "plugin/../../outside"])
    def test_a_plugin_path_that_climbs_out_of_the_bundle_is_refused(
        self, tmp_path: Path, path: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        bundle = plugin_bundle(tmp_path)
        outside = tmp_path / "outside"
        (outside / ".claude-plugin").mkdir(parents=True)
        (outside / ".claude-plugin" / "plugin.json").write_text("{}")
        plugins = tmp_path / "plugins"

        with caplog.at_level(logging.ERROR):
            ExtensionPlugin(plugins_dir=plugins).install(carrying(path), bundle)

        assert not (plugins / "acme.notes").exists()
        assert "not in its bundle" in caplog.text

    def test_a_symlink_out_of_the_bundle_is_refused(self, tmp_path: Path) -> None:
        bundle = plugin_bundle(tmp_path)
        outside = tmp_path / "elsewhere"
        (outside / ".claude-plugin").mkdir(parents=True)
        (outside / ".claude-plugin" / "plugin.json").write_text("{}")
        (bundle / "linked").symlink_to(outside, target_is_directory=True)
        plugins = tmp_path / "plugins"

        ExtensionPlugin(plugins_dir=plugins).install(carrying("linked"), bundle)

        assert not (plugins / "acme.notes").exists()

    def test_a_plugin_that_is_not_a_directory_is_refused(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bundle = plugin_bundle(tmp_path)
        (bundle / "file-plugin").write_text("x")
        plugins = tmp_path / "plugins"

        with caplog.at_level(logging.ERROR):
            ExtensionPlugin(plugins_dir=plugins).install(carrying("file-plugin"), bundle)

        assert not (plugins / "acme.notes").exists()
        assert "not in its bundle" in caplog.text

    def test_a_plugin_without_its_plugin_json_is_refused_and_says_why(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugins = tmp_path / "plugins"

        with caplog.at_level(logging.ERROR):
            ExtensionPlugin(plugins_dir=plugins).install(
                carrying(), plugin_bundle(tmp_path, manifest_file=False)
            )

        assert not (plugins / "acme.notes").exists()
        assert ".claude-plugin/plugin.json" in caplog.text

    def test_a_copy_that_fails_is_logged_and_never_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugins = tmp_path / "plugins"
        plugins.write_text("a file where the directory should be")

        with caplog.at_level(logging.ERROR):
            ExtensionPlugin(plugins_dir=plugins).install(carrying(), plugin_bundle(tmp_path))

        assert "installing acme.notes's plugin failed" in caplog.text

    def test_removing_deletes_the_copy_and_only_that_one(self, tmp_path: Path) -> None:
        plugins = tmp_path / "plugins"
        plugin = ExtensionPlugin(plugins_dir=plugins)
        plugin.install(carrying(), plugin_bundle(tmp_path))
        (plugins / "other.ext").mkdir()

        plugin.remove("acme.notes")

        assert not (plugins / "acme.notes").exists()
        assert (plugins / "other.ext").is_dir()

    def test_removing_what_was_never_installed_is_harmless_and_says_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            ExtensionPlugin(plugins_dir=tmp_path / "plugins").remove("acme.notes")
            ExtensionPlugin().remove("acme.notes")

        assert "plugin" not in caplog.text


class TestToolAudience:
    def test_own_agents_may_use_the_service_and_nobody_else(self, tmp_path: Path) -> None:
        audience = ToolAudience()
        plugins = tmp_path / "plugins"
        plugin = ExtensionPlugin(plugins_dir=plugins, audience=audience)
        extension = carrying(toolAudience="own-agents")
        server = server_name("acme.notes")

        plugin.restrict(extension)

        assert audience.allows(server, plugins / "acme.notes" / "agents" / "a.md")
        assert not audience.allows(server, plugins / "other.ext" / "agents" / "a.md")
        assert not audience.allows(server, None)

    def test_without_a_plugins_directory_own_agents_fails_closed(self, tmp_path: Path) -> None:
        audience = ToolAudience()
        extension = carrying(toolAudience="own-agents")

        ExtensionPlugin(audience=audience).restrict(extension)

        server = server_name("acme.notes")
        assert not audience.allows(server, tmp_path / "plugins" / "acme.notes" / "a.md")
        assert not audience.allows(server, None)

    def test_all_agents_is_no_restriction(self, tmp_path: Path) -> None:
        audience = ToolAudience()

        ExtensionPlugin(plugins_dir=tmp_path, audience=audience).restrict(carrying())

        assert audience.allows(server_name("acme.notes"), None)

    def test_releasing_lifts_the_restriction(self, tmp_path: Path) -> None:
        audience = ToolAudience()
        plugin = ExtensionPlugin(plugins_dir=tmp_path, audience=audience)
        plugin.restrict(carrying(toolAudience="own-agents"))

        plugin.release("acme.notes")

        assert audience.allows(server_name("acme.notes"), None)
