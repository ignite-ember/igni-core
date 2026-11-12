"""``/plugin`` and ``/plugins`` pinned through the command, router, verb and gateway.

Each test drives a real :class:`CommandHandler` (or a real
:class:`PluginBackendGateway`) and replaces only what would leave the
process: the installer and the marketplace store are injected through the
gateway's own seam, so what is asserted is what the user is told and what
state is left behind, not that an inner call happened.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from igni_core.backend.cmd_plugin import commands as commands_module
from igni_core.backend.cmd_plugin.bulk_refresh import BulkRefreshRunner
from igni_core.backend.cmd_plugin.commands import PluginCommand, PluginsCommand
from igni_core.backend.cmd_plugin.context import SlashCommandContext
from igni_core.backend.cmd_plugin.gateway import PluginBackendGateway
from igni_core.backend.cmd_plugin.routers import (
    MarketplaceRouter,
    PluginInstallRouter,
    PluginsToggleRouter,
)
from igni_core.backend.command_handler import CommandHandler
from igni_core.core.plugins.git import GitError
from igni_core.core.plugins.installer import PluginError, PluginInstaller
from igni_core.core.plugins.marketplace_store import MarketplaceRegistryStore
from igni_core.core.plugins.models import (
    MarketplaceCatalog,
    MarketplaceEntry,
    MarketplacePluginEntry,
    MarketplaceRegistry,
    ResolvedSource,
)
from igni_core.core.plugins.source_policy import PluginSourcePolicy
from igni_core.core.plugins.state import PluginsState, load_state
from igni_core.core.session.schemas import PluginReloadCounts

PUBLIC = "https://github.com/someone/plugins.git"


def _run(coro):
    return asyncio.run(coro)


# ── Collaborators injected through the gateway's seam ──────────────


class FakeInstaller:
    """Stands in for :class:`PluginInstaller`; raises what it is told to."""

    def __init__(self, *, git: bool = True, raises: Exception | None = None, sha: str = "a" * 40):
        self.git = git
        self.raises = raises
        self.sha = sha
        self.calls: list[tuple] = []
        self.manifest = SimpleNamespace(name="alpha", version="1.2.3")

    def is_git_available(self) -> bool:
        return self.git

    def install(self, url, *, ref, subdir):
        self.calls.append(("install", url, ref, subdir))
        if self.raises:
            raise self.raises
        return self.manifest

    def update(self, name, *, ref):
        self.calls.append(("update", name, ref))
        if self.raises:
            raise self.raises
        return self.sha

    def remove(self, name):
        self.calls.append(("remove", name))
        if self.raises:
            raise self.raises


class FakeStore:
    """Stands in for :class:`MarketplaceRegistryStore`."""

    def __init__(self, entries: list[MarketplaceEntry] | None = None):
        self.registry = MarketplaceRegistry(marketplaces=list(entries or []))
        self.raises: Exception | None = None
        self.refresh_returns: MarketplaceEntry | None = None
        self.refresh_calls: list[str] = []
        self.add_calls: list[str] = []
        self.resolved = None

    def load(self):
        return self.registry

    def add(self, url):
        self.add_calls.append(url)
        if self.raises:
            raise self.raises
        return _entry("fresh", 3)

    def remove(self, name):
        return self.registry.remove(name)

    def refresh(self, name):
        self.refresh_calls.append(name)
        if self.raises:
            raise self.raises
        return self.refresh_returns

    def resolve_install_ref(self, target):
        return self.resolved


def _entry(name: str, plugins: int = 0, url: str | None = None) -> MarketplaceEntry:
    entry = MarketplaceEntry(name=name, url=url or f"https://m/{name}.git")
    entry.mark_fetched(
        MarketplaceCatalog(
            name=name,
            plugins=[MarketplacePluginEntry(name=f"p{i}") for i in range(plugins)],
        )
    )
    return entry


def _gateway(installer=None, store=None, policy=None) -> PluginBackendGateway:
    return PluginBackendGateway(
        "/unused",
        policy,
        installer=installer or FakeInstaller(),
        marketplaces=store or FakeStore(),
    )


AIR_GAPPED = PluginSourcePolicy(air_gapped=True)


# ── Gateway: install / update / remove ─────────────────────────────


def test_an_air_gapped_gateway_refuses_a_public_install_and_never_calls_the_installer() -> None:
    installer = FakeInstaller()
    result = _gateway(installer, policy=AIR_GAPPED).install(PUBLIC, ref=None, subdir=None)
    assert not result.ok
    assert result.error == AIR_GAPPED.refusal(PUBLIC)
    assert installer.calls == []


def test_an_air_gapped_gateway_still_installs_from_a_local_path() -> None:
    installer = FakeInstaller()
    result = _gateway(installer, policy=AIR_GAPPED).install(
        "/srv/plugins/alpha", ref="v1", subdir="s"
    )
    assert result.ok
    assert installer.calls == [("install", "/srv/plugins/alpha", "v1", "s")]


def test_install_reports_the_manifest_name_and_an_empty_version_when_it_has_none() -> None:
    installer = FakeInstaller()
    installer.manifest = SimpleNamespace(name="alpha", version=None)
    result = _gateway(installer).install(PUBLIC, ref=None, subdir=None)
    assert (result.ok, result.name, result.version) == (True, "alpha", "")


def test_install_maps_a_git_error_and_a_plugin_error_to_their_texts() -> None:
    git = _gateway(FakeInstaller(raises=GitError("clone failed"))).install(
        PUBLIC, ref=None, subdir=None
    )
    assert (git.ok, git.error) == (False, "git error: clone failed")
    plugin = _gateway(FakeInstaller(raises=PluginError("bad manifest"))).install(
        PUBLIC, ref=None, subdir=None
    )
    assert (plugin.ok, plugin.error) == (False, "bad manifest")


def test_an_unexpected_exception_is_not_swallowed_by_the_gateway() -> None:
    with pytest.raises(RuntimeError):
        _gateway(FakeInstaller(raises=RuntimeError("bug"))).install(PUBLIC, ref=None, subdir=None)
    with pytest.raises(RuntimeError):
        _gateway(FakeInstaller(raises=RuntimeError("bug"))).update("a", ref=None)


def test_update_returns_the_new_sha_or_the_mapped_error() -> None:
    ok = _gateway(FakeInstaller(sha="b" * 40)).update("alpha", ref="main")
    assert (ok.ok, ok.sha) == (True, "b" * 40)
    git = _gateway(FakeInstaller(raises=GitError("fetch failed"))).update("alpha", ref=None)
    assert git.error == "git error: fetch failed"
    plugin = _gateway(FakeInstaller(raises=PluginError("not installed"))).update("alpha", ref=None)
    assert plugin.error == "not installed"


def test_remove_returns_ok_or_the_plugin_error() -> None:
    assert _gateway().remove("alpha").ok
    failed = _gateway(FakeInstaller(raises=PluginError("not installed"))).remove("alpha")
    assert (failed.ok, failed.error) == (False, "not installed")


# ── Gateway: marketplaces ──────────────────────────────────────────


def test_an_air_gapped_gateway_refuses_to_add_a_public_marketplace_without_fetching() -> None:
    store = FakeStore()
    result = _gateway(store=store, policy=AIR_GAPPED).add_marketplace(PUBLIC)
    assert (result.ok, result.error) == (False, AIR_GAPPED.refusal(PUBLIC))
    assert store.add_calls == []


def test_add_marketplace_reports_name_and_plugin_count() -> None:
    result = _gateway().add_marketplace(PUBLIC)
    assert (result.ok, result.name, result.plugin_count) == (True, "fresh", 3)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (GitError("no route"), "git error: no route"),
        (PluginError("bad"), "Failed to add marketplace: bad"),
        (ValueError("bad catalog"), "Failed to add marketplace: bad catalog"),
        (OSError("disk full"), "Failed to add marketplace: disk full"),
    ],
)
def test_add_marketplace_failures_carry_their_own_prefix(raised, expected) -> None:
    store = FakeStore()
    store.raises = raised
    result = _gateway(store=store).add_marketplace(PUBLIC)
    assert (result.ok, result.error) == (False, expected)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (GitError("no route"), "git error: no route"),
        (PluginError("bad"), "Refresh failed: bad"),
        (ValueError("bad catalog"), "Refresh failed: bad catalog"),
        (OSError("disk full"), "Refresh failed: disk full"),
    ],
)
def test_refresh_marketplace_failures_carry_their_own_prefix(raised, expected) -> None:
    store = FakeStore([_entry("m")])
    store.raises = raised
    result = _gateway(store=store).refresh_marketplace("m")
    assert (result.ok, result.not_found, result.error) == (False, False, expected)


def test_refresh_of_an_unknown_marketplace_is_not_found() -> None:
    result = _gateway().refresh_marketplace("ghost")
    assert (result.ok, result.not_found, result.name) == (False, True, "ghost")


def test_refresh_reports_name_and_plugin_count_on_success() -> None:
    store = FakeStore([_entry("m", 2)])
    store.refresh_returns = _entry("m", 2)
    result = _gateway(store=store).refresh_marketplace("m")
    assert (result.ok, result.name, result.plugin_count) == (True, "m", 2)


def test_a_marketplace_registered_before_the_air_gap_is_not_refreshed_after_it() -> None:
    store = FakeStore([_entry("old", 1, url=PUBLIC)])
    result = _gateway(store=store, policy=AIR_GAPPED).refresh_marketplace("old")
    assert (result.ok, result.error) == (False, AIR_GAPPED.refusal(PUBLIC))
    assert store.refresh_calls == []


def test_an_unexpected_refresh_exception_propagates() -> None:
    store = FakeStore([_entry("m")])
    store.raises = RuntimeError("bug")
    with pytest.raises(RuntimeError):
        _gateway(store=store).refresh_marketplace("m")


def test_resolve_install_ref_maps_a_hit_and_returns_none_for_a_miss() -> None:
    store = FakeStore()
    gateway = _gateway(store=store)
    assert gateway.resolve_install_ref("@m/p") is None
    store.resolved = (
        ResolvedSource(kind="git-subdir", url="https://x/y.git", subdir="sub", ref="v2"),
        None,
    )
    resolved = gateway.resolve_install_ref("@m/p")
    assert (resolved.url, resolved.subdir, resolved.ref) == ("https://x/y.git", "sub", "v2")


def test_the_registry_is_empty_for_a_missing_or_corrupt_file_and_remove_says_whether_it_hit(
    tmp_path: Path,
) -> None:
    gateway = PluginBackendGateway(str(tmp_path))
    assert gateway.list_marketplaces().marketplaces == []
    (tmp_path / "marketplaces.json").write_text("{not json", encoding="utf-8")
    assert gateway.list_marketplaces().marketplaces == []
    MarketplaceRegistryStore(tmp_path).save(MarketplaceRegistry(marketplaces=[_entry("m")]))
    assert gateway.remove_marketplace("ghost") is False
    assert gateway.remove_marketplace("m") is True
    assert gateway.list_marketplaces().marketplaces == []


def test_from_settings_reads_the_data_dir_and_the_source_policy(tmp_path: Path) -> None:
    MarketplaceRegistryStore(tmp_path).save(MarketplaceRegistry(marketplaces=[_entry("kept")]))
    settings = SimpleNamespace(
        storage=SimpleNamespace(data_dir=str(tmp_path)),
        air_gapped=True,
        plugins=SimpleNamespace(marketplaces=[], default_marketplace=False),
    )
    gateway = PluginBackendGateway.from_settings(settings)
    assert [m.name for m in gateway.list_marketplaces().marketplaces] == ["kept"]
    assert gateway.add_marketplace(PUBLIC).error == PluginSourcePolicy(air_gapped=True).refusal(
        PUBLIC
    )
    assert gateway.install(PUBLIC, ref=None, subdir=None).ok is False


def test_a_gateway_built_without_a_policy_is_not_air_gapped() -> None:
    store = FakeStore()
    assert _gateway(store=store).add_marketplace(PUBLIC).ok


def test_the_gateway_builds_one_installer_and_hands_it_the_data_dir_and_policy(
    tmp_path: Path,
) -> None:
    built: list[dict] = []

    class Recording(FakeInstaller):
        def __init__(self, **kwargs) -> None:
            super().__init__()
            built.append(kwargs)

    policy = PluginSourcePolicy(air_gapped=True)
    with patch("igni_core.backend.cmd_plugin.gateway.PluginInstaller", Recording):
        gateway = PluginBackendGateway(str(tmp_path), policy)
        gateway.install("/local/a", ref=None, subdir=None)
        gateway.install("/local/b", ref=None, subdir=None)
        gateway.remove("a")
    assert built == [{"data_dir": str(tmp_path), "source_policy": policy}]


# ── Bulk refresh ───────────────────────────────────────────────────


def test_bulk_refresh_of_an_empty_registry_is_empty() -> None:
    result = BulkRefreshRunner(_gateway()).run(MarketplaceRegistry())
    assert result.ok == [] and result.failed == [] and result.is_empty()


def test_bulk_refresh_keeps_going_after_a_failure_and_names_every_outcome_in_order() -> None:
    registry = MarketplaceRegistry(
        marketplaces=[_entry("a"), _entry("bad"), _entry("c"), _entry("gone")]
    )

    class Store(FakeStore):
        def refresh(self, name):
            self.refresh_calls.append(name)
            if name == "bad":
                raise GitError("timeout")
            if name == "gone":
                return None
            return _entry(name)

    store = Store()
    store.registry = registry
    result = BulkRefreshRunner(_gateway(store=store)).run(registry)
    assert store.refresh_calls == ["a", "bad", "c", "gone"]
    assert result.ok == ["a", "c"]
    assert [(f.name, f.reason) for f in result.failed] == [
        ("bad", "git error: timeout"),
        ("gone", "not registered"),
    ]


def test_bulk_refresh_where_everything_fails_has_no_ok_entries() -> None:
    store = FakeStore([_entry("a"), _entry("b")])
    store.raises = OSError("disk")
    result = BulkRefreshRunner(_gateway(store=store)).run(store.registry)
    assert result.ok == []
    assert [f.reason for f in result.failed] == ["Refresh failed: disk"] * 2


def test_bulk_refresh_lets_an_unexpected_exception_abort_the_run() -> None:
    store = FakeStore([_entry("a")])
    store.raises = RuntimeError("bug")
    with pytest.raises(RuntimeError):
        BulkRefreshRunner(_gateway(store=store)).run(store.registry)


# ── The command layer ──────────────────────────────────────────────


def _session(tmp_path: Path, *, reload_counts=None) -> MagicMock:
    session = MagicMock()
    session.settings.storage.data_dir = str(tmp_path)
    session.plugin_state = PluginsState()
    session.reload_plugins.return_value = reload_counts or PluginReloadCounts(
        plugins=1, skills=2, agents=3, hooks=4
    )
    return session


def _handler(monkeypatch, session, gateway: PluginBackendGateway) -> CommandHandler:
    monkeypatch.setattr(
        PluginBackendGateway, "from_settings", classmethod(lambda cls, settings: gateway)
    )
    return CommandHandler(session)


def _say(handler: CommandHandler, line: str):
    return _run(handler.handle(line))


def test_the_command_classes_are_built_without_arguments_and_named(tmp_path: Path) -> None:
    plugin, plugins = PluginCommand(), PluginsCommand()
    assert (plugin.name, plugin.description) == ("/plugin", "Install / update / remove a plugin")
    assert (plugins.name, plugins.description) == ("/plugins", "Open the plugins panel")


def test_plugin_without_arguments_is_an_error_carrying_the_usage(monkeypatch, tmp_path) -> None:
    result = _say(_handler(monkeypatch, _session(tmp_path), _gateway()), "/plugin   ")
    assert result.kind == "error"
    assert result.content.startswith("Usage: /plugin install <git-url|@marketplace/plugin>")
    assert "/plugin marketplace add|list|remove|refresh" in result.content


def test_plugin_subcommands_are_case_insensitive(monkeypatch, tmp_path) -> None:
    installer = FakeInstaller()
    handler = _handler(monkeypatch, _session(tmp_path), _gateway(installer))
    assert _say(handler, "/plugin INSTALL /local/a").kind == "info"
    assert installer.calls == [("install", "/local/a", None, None)]
    listing = _say(handler, "/plugin Marketplace LIST")
    assert listing.kind == "markdown"


def test_an_unknown_plugin_subcommand_is_echoed_lowercased(monkeypatch, tmp_path) -> None:
    handler = _handler(monkeypatch, _session(tmp_path), _gateway())
    result = _say(handler, "/plugin FooBar x")
    assert result.kind == "error"
    assert result.content == (
        "Unknown /plugin subcommand: 'foobar'. Use install / update / remove / marketplace."
    )


def test_a_marketplace_verb_is_not_an_install_subcommand_and_the_reverse(
    monkeypatch, tmp_path
) -> None:
    handler = _handler(monkeypatch, _session(tmp_path), _gateway())
    assert "Unknown /plugin subcommand: 'list'" in _say(handler, "/plugin list").content
    wrong = _say(handler, "/plugin marketplace install x")
    assert wrong.content == (
        "Unknown /plugin marketplace action: 'install'. Use add / list / remove / refresh."
    )


def test_marketplace_without_an_action_is_the_marketplace_usage(monkeypatch, tmp_path) -> None:
    result = _say(_handler(monkeypatch, _session(tmp_path), _gateway()), "/plugin marketplace")
    assert result.content == (
        "Usage: /plugin marketplace add <url> | list | remove <name> | refresh [<name>]"
    )


def test_every_command_builds_its_gateway_from_the_settings_as_they_are_then(tmp_path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    MarketplaceRegistryStore(first).save(MarketplaceRegistry(marketplaces=[_entry("in-one")]))
    MarketplaceRegistryStore(second).save(MarketplaceRegistry(marketplaces=[_entry("in-two")]))
    session = _session(first)
    handler = CommandHandler(session)
    assert "in-one" in _say(handler, "/plugin marketplace list").content
    session.settings.storage.data_dir = str(second)
    after = _say(handler, "/plugin marketplace list").content
    assert "in-two" in after and "in-one" not in after


def test_the_air_gap_in_the_settings_reaches_a_plugin_install(tmp_path) -> None:
    session = _session(tmp_path)
    session.settings.air_gapped = True
    handler = CommandHandler(session)
    with (
        patch.object(PluginInstaller, "is_git_available", return_value=True),
        patch.object(PluginInstaller, "install", side_effect=AssertionError("git reached")),
    ):
        result = _say(handler, f"/plugin install {PUBLIC}")
    assert result.kind == "error"
    assert result.content == PluginSourcePolicy(air_gapped=True).refusal(PUBLIC)
    session.reload_plugins.assert_not_called()


def test_the_context_is_frozen_and_holds_the_live_session(tmp_path) -> None:
    session, gateway = _session(tmp_path), _gateway()
    ctx = SlashCommandContext(session=session, plugin_data_dir="/d", gateway=gateway)
    assert ctx.session is session
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.plugin_data_dir = "/other"  # type: ignore[misc]


def test_the_routers_hold_exactly_their_own_verbs() -> None:
    assert set(PluginInstallRouter()._verbs) == {"install", "update", "remove"}
    assert set(MarketplaceRouter()._verbs) == {"add", "list", "remove", "refresh"}
    assert set(PluginsToggleRouter()._verbs) == {"enable", "disable"}


def test_the_plugin_package_speaks_of_igni_only() -> None:
    package = Path(commands_module.__file__).parent
    old = re.compile(r"(?<![a-z])ember", re.IGNORECASE)
    hits = [p.name for p in package.glob("*.py") if old.search(p.read_text(encoding="utf-8"))]
    assert hits == []
    assert commands_module.__name__ == "igni_core.backend.cmd_plugin.commands"


# ── Verbs: install / update / remove ───────────────────────────────


def test_install_without_git_is_refused_before_anything_runs(monkeypatch, tmp_path) -> None:
    installer = FakeInstaller(git=False)
    session = _session(tmp_path)
    result = _say(_handler(monkeypatch, session, _gateway(installer)), "/plugin install /x")
    assert result.content == "`git` is not on PATH. Install git, then retry."
    assert installer.calls == []
    session.reload_plugins.assert_not_called()


def test_install_success_names_the_plugin_version_ref_and_the_reload_counts(
    monkeypatch, tmp_path
) -> None:
    store = FakeStore()
    store.resolved = (
        ResolvedSource(kind="git-subdir", url="https://x/y.git", subdir="sub", ref="v9"),
        None,
    )
    installer = FakeInstaller()
    session = _session(tmp_path)
    handler = _handler(monkeypatch, session, _gateway(installer, store))
    result = _say(handler, "/plugin install @m/alpha")
    assert result.kind == "info"
    assert result.content == (
        "Installed plugin 'alpha' v1.2.3 via @m/alpha. "
        "Active now — 2 skill(s), 3 agent(s), 4 hook(s). "
        "Any bundled MCP servers are starting in the background."
    )
    assert installer.calls == [("install", "https://x/y.git", "v9", "sub")]
    session.reload_plugins.assert_called_once()


def test_an_explicit_ref_wins_over_the_marketplace_declared_one(monkeypatch, tmp_path) -> None:
    store = FakeStore()
    store.resolved = (
        ResolvedSource(kind="git-subdir", url="https://x/y.git", subdir=None, ref="v9"),
        None,
    )
    installer = FakeInstaller()
    handler = _handler(monkeypatch, _session(tmp_path), _gateway(installer, store))
    _say(handler, "/plugin install @m/alpha --ref mine")
    assert installer.calls[0][2] == "mine"


def test_a_bare_url_install_omits_the_version_when_the_manifest_has_none(
    monkeypatch, tmp_path
) -> None:
    installer = FakeInstaller()
    installer.manifest = SimpleNamespace(name="alpha", version="")
    handler = _handler(monkeypatch, _session(tmp_path), _gateway(installer))
    result = _say(handler, "/plugin install /x --ref main")
    assert result.content.startswith("Installed plugin 'alpha'. Active now")
    assert installer.calls == [("install", "/x", "main", None)]


def test_an_unresolvable_marketplace_ref_is_explained(monkeypatch, tmp_path) -> None:
    installer = FakeInstaller()
    handler = _handler(monkeypatch, _session(tmp_path), _gateway(installer))
    result = _say(handler, "/plugin install @m/ghost")
    assert result.kind == "error"
    assert result.content.startswith("Could not resolve '@m/ghost'.")
    assert "/plugin marketplace list" in result.content
    assert installer.calls == []


def test_a_failed_install_update_or_remove_does_not_reload_plugins(monkeypatch, tmp_path) -> None:
    session = _session(tmp_path)
    installer = FakeInstaller(raises=PluginError("boom"))
    handler = _handler(monkeypatch, session, _gateway(installer))
    for line in ("/plugin install /x", "/plugin update a", "/plugin remove a"):
        result = _say(handler, line)
        assert (result.kind, result.content) == ("error", "boom")
    session.reload_plugins.assert_not_called()


def test_update_without_git_is_refused(monkeypatch, tmp_path) -> None:
    installer = FakeInstaller(git=False)
    result = _say(
        _handler(monkeypatch, _session(tmp_path), _gateway(installer)), "/plugin update a"
    )
    assert result.content == "`git` is not on PATH. Install git, then retry."
    assert installer.calls == []


def test_update_reports_the_first_twelve_characters_of_the_sha(monkeypatch, tmp_path) -> None:
    session = _session(tmp_path)
    installer = FakeInstaller(sha="0123456789abcdef0123")
    handler = _handler(monkeypatch, session, _gateway(installer))
    assert _say(handler, "/plugin update alpha --ref main").content == (
        "Updated 'alpha' to 0123456789ab. Active now."
    )
    installer.sha = "abc"
    assert _say(handler, "/plugin update alpha").content == "Updated 'alpha' to abc. Active now."
    assert session.reload_plugins.call_count == 2


def test_remove_reports_removal_and_reloads(monkeypatch, tmp_path) -> None:
    session = _session(tmp_path)
    handler = _handler(monkeypatch, session, _gateway())
    result = _say(handler, "/plugin remove alpha")
    assert result.content == (
        "Removed 'alpha'. Skills/agents/hooks/tools no longer "
        "active; bundled MCP servers are being disconnected."
    )
    session.reload_plugins.assert_called_once()


def test_an_unexpected_installer_exception_surfaces_instead_of_a_generic_message(
    monkeypatch, tmp_path
) -> None:
    handler = _handler(
        monkeypatch, _session(tmp_path), _gateway(FakeInstaller(raises=RuntimeError("bug")))
    )
    with pytest.raises(RuntimeError):
        _say(handler, "/plugin remove a")


# ── Verbs: marketplace ─────────────────────────────────────────────


def test_marketplace_add_reports_name_url_and_count(monkeypatch, tmp_path) -> None:
    handler = _handler(monkeypatch, _session(tmp_path), _gateway())
    result = _say(handler, f"/plugin marketplace add {PUBLIC}")
    assert result.content == f"Added marketplace 'fresh' from {PUBLIC} (3 plugin(s) catalogued)."


def test_marketplace_add_surfaces_the_failure(monkeypatch, tmp_path) -> None:
    store = FakeStore()
    store.raises = ValueError("no catalog")
    result = _say(
        _handler(monkeypatch, _session(tmp_path), _gateway(store=store)),
        "/plugin marketplace add x y",
    )
    assert result.content == "Usage: /plugin marketplace add <git-url>"
    result = _say(
        _handler(monkeypatch, _session(tmp_path), _gateway(store=store)),
        "/plugin marketplace add x",
    )
    assert result.content == "Failed to add marketplace: no catalog"


def test_marketplace_list_renders_each_entry_and_never_for_an_unfetched_one(
    monkeypatch, tmp_path
) -> None:
    unfetched = MarketplaceEntry(name="raw", url="https://m/raw.git")
    store = FakeStore([_entry("m", 2), unfetched])
    handler = _handler(monkeypatch, _session(tmp_path), _gateway(store=store))
    result = _say(handler, "/plugin marketplace list")
    fetched = store.registry.marketplaces[0].last_fetched
    assert result.content == (
        "## Marketplaces\n"
        f"- **m** · 2 plugin(s) · last fetched {fetched}\n  - https://m/m.git\n"
        "- **raw** · 0 plugin(s) · last fetched never\n  - https://m/raw.git"
    )


def test_marketplace_list_of_an_empty_registry_says_how_to_add_one(monkeypatch, tmp_path) -> None:
    result = _say(_handler(monkeypatch, _session(tmp_path), _gateway()), "/plugin marketplace list")
    assert result.content == (
        "## Marketplaces\n(none registered — add one via `/plugin marketplace add <git-url>`)"
    )


def test_marketplace_remove_keeps_installed_plugins_and_rejects_an_unknown_name(
    monkeypatch, tmp_path
) -> None:
    store = FakeStore([_entry("m")])
    handler = _handler(monkeypatch, _session(tmp_path), _gateway(store=store))
    done = _say(handler, "/plugin marketplace remove m")
    assert done.content == (
        "Unregistered marketplace 'm'. Installed plugins from it remain installed."
    )
    assert store.registry.marketplaces == []
    missing = _say(handler, "/plugin marketplace remove m")
    assert (missing.kind, missing.content) == ("error", "No marketplace named 'm' is registered.")


def test_a_named_refresh_reports_unknown_failed_and_refreshed(monkeypatch, tmp_path) -> None:
    store = FakeStore([_entry("m", 4)])
    handler = _handler(monkeypatch, _session(tmp_path), _gateway(store=store))
    assert _say(handler, "/plugin marketplace refresh ghost").content == (
        "No marketplace named 'ghost' is registered."
    )
    store.raises = GitError("down")
    assert _say(handler, "/plugin marketplace refresh m").content == "git error: down"
    store.raises = None
    store.refresh_returns = _entry("m", 4)
    assert _say(handler, "/plugin marketplace refresh m").content == "Refreshed 'm' (4 plugin(s))."


def test_refreshing_all_with_nothing_registered_says_so(monkeypatch, tmp_path) -> None:
    result = _say(
        _handler(monkeypatch, _session(tmp_path), _gateway()), "/plugin marketplace refresh"
    )
    assert (result.kind, result.content) == ("info", "No marketplaces to refresh.")


def test_refreshing_all_summarises_ok_and_failed_as_markdown(monkeypatch, tmp_path) -> None:
    class Store(FakeStore):
        def refresh(self, name):
            if name == "bad":
                raise PluginError("broken")
            return _entry(name)

    store = Store([_entry("good"), _entry("bad")])
    result = _say(
        _handler(monkeypatch, _session(tmp_path), _gateway(store=store)),
        "/plugin marketplace refresh",
    )
    assert result.kind == "markdown"
    assert result.content == (
        "## Marketplace refresh\n- good: ok\n- bad: failed (Refresh failed: broken)"
    )


# ── /plugins ───────────────────────────────────────────────────────


def _toggle_session(tmp_path: Path, names: list[str], disabled: list[str]) -> MagicMock:
    session = _session(tmp_path)
    session.plugin_state = PluginsState(disabled=list(disabled))
    session.plugin_loader.get.side_effect = lambda n: object() if n in names else None
    return session


def test_plugins_toggle_subcommand_is_case_insensitive_and_the_name_stays_one_string(
    monkeypatch, tmp_path
) -> None:
    session = _toggle_session(tmp_path, ["two words"], [])
    handler = _handler(monkeypatch, session, _gateway())
    result = _say(handler, "/plugins DISABLE   two words  ")
    assert result.kind == "info"
    assert result.content.startswith("Plugin 'two words' disabled. ")
    assert session.plugin_state.disabled == ["two words"]


def test_a_toggle_keeps_the_disabled_list_sorted_without_duplicates_and_persists_it(
    monkeypatch, tmp_path
) -> None:
    session = _toggle_session(tmp_path, ["a", "m", "z"], ["z", "a"])
    handler = _handler(monkeypatch, session, _gateway())
    _say(handler, "/plugins disable m")
    assert session.plugin_state.disabled == ["a", "m", "z"]
    assert load_state(data_dir=tmp_path).disabled == ["a", "m", "z"]
    result = _say(handler, "/plugins enable a")
    assert result.content == (
        "Plugin 'a' enabled. Its skills/agents/hooks/tools are active; any "
        "bundled MCP servers are starting in the background."
    )
    assert load_state(data_dir=tmp_path).disabled == ["m", "z"]
    assert session.reload_plugins.call_count == 2


def test_the_disable_message_says_the_servers_are_disconnecting(monkeypatch, tmp_path) -> None:
    session = _toggle_session(tmp_path, ["a"], [])
    result = _say(_handler(monkeypatch, session, _gateway()), "/plugins disable a")
    assert result.content == (
        "Plugin 'a' disabled. Its skills/agents/hooks/tools are no longer active; "
        "any bundled MCP servers are being disconnected."
    )


def test_a_toggle_that_changes_nothing_neither_saves_nor_reloads(monkeypatch, tmp_path) -> None:
    session = _toggle_session(tmp_path, ["a"], ["a"])
    handler = _handler(monkeypatch, session, _gateway())
    result = _say(handler, "/plugins disable a")
    assert result.content == "Plugin 'a' is already disabled."
    assert not (tmp_path / "plugins.json").exists()
    session.reload_plugins.assert_not_called()


def test_a_toggle_of_an_unknown_plugin_changes_nothing(monkeypatch, tmp_path) -> None:
    session = _toggle_session(tmp_path, [], [])
    result = _say(_handler(monkeypatch, session, _gateway()), "/plugins enable ghost")
    assert result.content == (
        "No plugin named 'ghost' is installed. Run `/plugins` to list installed plugins."
    )
    assert session.plugin_state.disabled == []
    session.reload_plugins.assert_not_called()


def test_plugins_with_an_unknown_subcommand_names_it_lowercased(monkeypatch, tmp_path) -> None:
    result = _say(_handler(monkeypatch, _session(tmp_path), _gateway()), "/plugins ROMP x")
    assert result.content == (
        "Unknown /plugins subcommand: 'romp'. Use `enable` or "
        "`disable`, or run `/plugins` alone to open the panel."
    )


def test_a_toggle_without_a_name_gives_the_verb_specific_usage(monkeypatch, tmp_path) -> None:
    handler = _handler(monkeypatch, _session(tmp_path), _gateway())
    assert _say(handler, "/plugins enable").content == "Usage: /plugins enable <plugin-name>"
    assert _say(handler, "/plugins disable   ").content == "Usage: /plugins disable <plugin-name>"
