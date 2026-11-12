"""The plugin and marketplace RPCs, called by their wire arguments, over real git.

The backend is a real one, built with ``HOME`` on the test's folder so
nothing lands in the person's own plugins. The plugin and the
marketplace are real local git repositories, so a preview, an install
at a tag, an update, a toggle and a removal each do what they do for a
person: this file reads the ``info`` texts, the installed list and the
files on disk after each call.

Pinned:

* the argument models require what they declare, ignore keys a newer
  client adds, default the optional refs to ``None`` and describe each field;
* ``preview_plugin`` reads a repository without installing it — at a
  branch, in a subfolder, a ``subdir`` argument beating one in ``source`` —
  and says why when it cannot;
* ``install_plugin`` installs from a git URL at a tag or the default
  branch, or from ``@marketplace/plugin``; ``update_plugin`` moves it;
  ``set_plugin_enabled`` and ``remove_plugin`` change the installed list;
* the marketplace calls add, list, refresh and forget, with the empty
  and unknown cases told in the ``info``;
* each call declares its duration and scope.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from igni_core.backend.rpc_binding import RpcArgsError, RpcBinding
from igni_core.backend.rpc_handlers import plugins as plugins_module
from igni_core.backend.rpc_handlers.plugins import PluginsRpcHandler
from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from tests.rpc_tables import rpc_table_for


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _plugin_files(root: Path, *, name: str, version: str, readme: str) -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version, "description": f"{name} plugin"})
    )
    (root / "skills" / "greet").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: Say hi\n---\nSay hi.\n"
    )
    (root / "README.md").write_text(readme)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A plugin repository: v1 tagged, then a second commit on main, and a plugin in ``nested/``."""
    root = tmp_path / "plugin-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _plugin_files(root, name="demo", version="1.0.0", readme="first readme")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    _git(root, "tag", "v1")
    _plugin_files(root, name="demo", version="2.0.0", readme="second readme")
    _plugin_files(root / "nested", name="inner", version="0.1.0", readme="inner readme")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "two")
    return root


@pytest.fixture
def backend(tmp_path, monkeypatch) -> BackendServer:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "proj"
    project.mkdir()
    return BackendServer(SettingsLoader.load(project_dir=project), project_dir=project)


@pytest.fixture
def table(backend):
    return rpc_table_for(backend, MagicMock())


async def _ask(table: dict, method: RpcMethod, wire: dict[str, Any] | None = None) -> Any:
    result = table[method](wire or {})
    return await result if hasattr(result, "__await__") else result


def _installed(backend: BackendServer) -> dict[str, Any]:
    return {p.name: p for p in backend.get_plugin_details()}


# ── the argument models ───────────────────────────────────────────

_MODELS: list[tuple[type, dict[str, Any]]] = [
    (plugins_module.PluginNameArgs, {"name": "n"}),
    (plugins_module.PreviewPluginArgs, {"source": "s"}),
    (plugins_module.PluginEnabledArgs, {"name": "n", "enabled": True}),
    (plugins_module.InstallPluginArgs, {"ref": "r"}),
    (plugins_module.UpdatePluginArgs, {"name": "n"}),
    (plugins_module.MarketplaceUrlArgs, {"url": "u"}),
    (plugins_module.MarketplaceNameArgs, {"name": "n"}),
    (plugins_module.RefreshMarketplacesArgs, {}),
]


@pytest.mark.parametrize(("model", "wire"), _MODELS, ids=lambda v: getattr(v, "__name__", ""))
def test_each_model_requires_what_it_declares_ignores_extras_and_describes_itself(model, wire):
    for field in wire:
        with pytest.raises(ValidationError, match=field):
            model.model_validate({k: v for k, v in wire.items() if k != field})
    model.model_validate({**wire, "from_the_future": 1})
    for name, field in model.model_fields.items():
        assert field.description, f"{model.__name__}.{name} has no description"


def test_the_optional_refs_default_to_none_and_the_flag_has_no_default():
    assert plugins_module.PreviewPluginArgs(source="s").branch is None
    assert plugins_module.PreviewPluginArgs(source="s").subdir is None
    assert plugins_module.InstallPluginArgs(ref="r").install_ref is None
    assert plugins_module.UpdatePluginArgs(name="n").install_ref is None
    assert plugins_module.RefreshMarketplacesArgs().name is None
    with pytest.raises(ValidationError, match="enabled"):
        plugins_module.PluginEnabledArgs(name="n")


# ── looking ───────────────────────────────────────────────────────


async def test_a_plugin_nobody_installed_is_an_error_in_the_answer_not_a_raise(table):
    assert await _ask(table, RpcMethod.GET_PLUGIN_DETAILS) == []

    contents = await _ask(table, RpcMethod.GET_PLUGIN_CONTENTS, {"name": "ghost"})

    assert contents.error == "Plugin 'ghost' not found"


async def test_a_preview_reads_the_repository_at_the_asked_branch_without_installing(
    backend, table, repo
):
    default = await _ask(table, RpcMethod.PREVIEW_PLUGIN, {"source": str(repo)})
    tagged = await _ask(table, RpcMethod.PREVIEW_PLUGIN, {"source": str(repo), "branch": "v1"})

    assert default.error == "" and default.readme == "second readme"
    assert [s.name for s in default.skills] == ["greet"]
    assert default.preview_source.url == str(repo)
    assert tagged.readme == "first readme"
    assert _installed(backend) == {}


async def test_a_subdir_argument_beats_the_one_in_the_source(table, repo):
    named_in_source = await _ask(table, RpcMethod.PREVIEW_PLUGIN, {"source": f"{repo} [nested]"})
    overridden = await _ask(
        table, RpcMethod.PREVIEW_PLUGIN, {"source": f"{repo} [nested]", "subdir": "."}
    )
    explicit = await _ask(
        table, RpcMethod.PREVIEW_PLUGIN, {"source": str(repo), "subdir": "nested"}
    )

    assert named_in_source.readme == "inner readme"
    assert explicit.readme == "inner readme"
    assert overridden.readme == "second readme"
    assert explicit.preview_source.subdir == "nested"


async def test_a_preview_that_cannot_clone_says_why(table, tmp_path):
    answer = await _ask(table, RpcMethod.PREVIEW_PLUGIN, {"source": str(tmp_path / "no-repo")})

    assert answer.error.startswith("git clone failed")
    assert answer.preview_source is None


async def test_a_preview_of_a_missing_subfolder_says_the_entry_may_be_stale(table, repo):
    answer = await _ask(table, RpcMethod.PREVIEW_PLUGIN, {"source": str(repo), "subdir": "nope"})

    assert "no 'nope' subdirectory" in answer.error


# ── the plugin's life ─────────────────────────────────────────────


async def test_a_plugin_is_installed_at_a_tag_moved_toggled_and_removed(backend, table, repo):
    first_commit = _git(repo, "rev-parse", "v1")

    installed = await _ask(table, RpcMethod.INSTALL_PLUGIN, {"ref": str(repo), "install_ref": "v1"})
    assert isinstance(installed, msg.Info)
    assert installed.text.startswith("Installed plugin 'demo' v1.0.0. Active now")
    assert _installed(backend)["demo"].pin == first_commit
    details = (await _ask(table, RpcMethod.GET_PLUGIN_DETAILS))[0]
    assert (details.name, details.version, details.enabled, details.has_skills) == (
        "demo",
        "1.0.0",
        True,
        True,
    )
    contents = await _ask(table, RpcMethod.GET_PLUGIN_CONTENTS, {"name": "demo"})
    assert contents.error == "" and contents.readme == "first readme"

    moved = await _ask(table, RpcMethod.UPDATE_PLUGIN, {"name": "demo", "install_ref": "main"})
    second_commit = _git(repo, "rev-parse", "main")
    assert moved.text == f"Updated 'demo' to {second_commit[:12]}. Active now."
    assert _installed(backend)["demo"].pin == second_commit
    back = await _ask(table, RpcMethod.UPDATE_PLUGIN, {"name": "demo", "install_ref": "v1"})
    assert back.text == f"Updated 'demo' to {first_commit[:12]}. Active now."

    off = await _ask(table, RpcMethod.SET_PLUGIN_ENABLED, {"name": "demo", "enabled": False})
    assert "disabled" in off.text
    assert _installed(backend)["demo"].enabled is False
    on = await _ask(table, RpcMethod.SET_PLUGIN_ENABLED, {"name": "demo", "enabled": True})
    assert "enabled" in on.text and _installed(backend)["demo"].enabled is True

    removed = await _ask(table, RpcMethod.REMOVE_PLUGIN, {"name": "demo"})
    assert removed.text.startswith("Removed 'demo'.")
    assert _installed(backend) == {}


async def test_an_update_without_a_ref_follows_the_default_branch(backend, table, repo):
    await _ask(table, RpcMethod.INSTALL_PLUGIN, {"ref": str(repo)})
    (repo / "README.md").write_text("third readme")
    _git(repo, "commit", "-qam", "three")

    moved = await _ask(table, RpcMethod.UPDATE_PLUGIN, {"name": "demo"})

    third = _git(repo, "rev-parse", "main")
    assert moved.text == f"Updated 'demo' to {third[:12]}. Active now."
    assert _installed(backend)["demo"].pin == third


async def test_installing_without_a_ref_takes_the_default_branch(backend, table, repo):
    info = await _ask(table, RpcMethod.INSTALL_PLUGIN, {"ref": str(repo)})

    assert info.text.startswith("Installed plugin 'demo' v2.0.0.")
    assert _installed(backend)["demo"].pin == _git(repo, "rev-parse", "main")


async def test_an_install_that_cannot_clone_says_so_and_installs_nothing(backend, table, tmp_path):
    info = await _ask(table, RpcMethod.INSTALL_PLUGIN, {"ref": str(tmp_path / "no-repo")})

    assert isinstance(info, msg.Info)
    assert not info.text.startswith("Installed")
    assert _installed(backend) == {}


async def test_an_unknown_marketplace_entry_and_an_unknown_plugin_are_told(table):
    unresolved = await _ask(table, RpcMethod.INSTALL_PLUGIN, {"ref": "@nobody/nothing"})
    assert unresolved.text.startswith("Could not resolve '@nobody/nothing'")

    for method, wire in (
        (RpcMethod.UPDATE_PLUGIN, {"name": "ghost"}),
        (RpcMethod.REMOVE_PLUGIN, {"name": "ghost"}),
        (RpcMethod.SET_PLUGIN_ENABLED, {"name": "ghost", "enabled": False}),
    ):
        info = await _ask(table, method, wire)
        assert "ghost" in info.text, method


# ── marketplaces ──────────────────────────────────────────────────


@pytest.fixture
def marketplace(tmp_path, repo) -> Path:
    root = tmp_path / "market-repo"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "bazaar", "plugins": [{"name": "demo", "source": str(repo)}]})
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "catalogue")
    return root


async def test_a_marketplace_is_added_listed_installed_from_refreshed_and_forgotten(
    backend, table, marketplace
):
    added = await _ask(table, RpcMethod.ADD_MARKETPLACE, {"url": str(marketplace)})
    assert isinstance(added, msg.Info) and "bazaar" in added.text and "1" in added.text
    listed = await _ask(table, RpcMethod.GET_MARKETPLACES)
    assert [m.name for m in listed] == ["bazaar"]

    installed = await _ask(table, RpcMethod.INSTALL_PLUGIN, {"ref": "@bazaar/demo"})
    assert installed.text.startswith("Installed plugin 'demo'")
    assert "demo" in _installed(backend)

    every = await _ask(table, RpcMethod.REFRESH_MARKETPLACES)
    empty = await _ask(table, RpcMethod.REFRESH_MARKETPLACES, {"name": ""})
    one = await _ask(table, RpcMethod.REFRESH_MARKETPLACES, {"name": "bazaar"})
    unknown = await _ask(table, RpcMethod.REFRESH_MARKETPLACES, {"name": "zzz"})
    assert every.text == empty.text == "Refreshed 1 ok"
    assert one.text == "Refreshed 'bazaar' (1 plugins)."
    assert unknown.text != every.text and "zzz" in unknown.text

    gone = await _ask(table, RpcMethod.REMOVE_MARKETPLACE, {"name": "bazaar"})
    assert gone.text == "Unregistered 'bazaar'. Installed plugins from it remain."
    assert await _ask(table, RpcMethod.GET_MARKETPLACES) == []
    again = await _ask(table, RpcMethod.REMOVE_MARKETPLACE, {"name": "bazaar"})
    assert again.text == "No marketplace named 'bazaar'."


async def test_a_marketplace_that_is_not_one_is_refused_in_the_info(table, repo):
    info = await _ask(table, RpcMethod.ADD_MARKETPLACE, {"url": str(repo)})

    assert info.text.startswith("Failed to add marketplace: No marketplace.json found")
    assert await _ask(table, RpcMethod.GET_MARKETPLACES) == []


async def test_with_no_marketplaces_a_refresh_says_there_are_none(table):
    info = await _ask(table, RpcMethod.REFRESH_MARKETPLACES)

    assert info.text == "No marketplaces to refresh."


def test_a_call_with_a_missing_argument_is_refused_by_name(table):
    with pytest.raises(RpcArgsError, match="install_plugin: 'ref' is required"):
        table[RpcMethod.INSTALL_PLUGIN]({"install_ref": "v1"})
    with pytest.raises(RpcArgsError, match="set_plugin_enabled: 'enabled' is required"):
        table[RpcMethod.SET_PLUGIN_ENABLED]({"name": "n"})
    with pytest.raises(RpcArgsError, match="add_marketplace: 'url' is required"):
        table[RpcMethod.ADD_MARKETPLACE]({})


# ── what each call declares ───────────────────────────────────────


def test_each_call_declares_how_long_it_takes_and_what_it_acts_on():
    bindings = RpcBinding.on(PluginsRpcHandler)
    long_calls = {
        RpcMethod.PREVIEW_PLUGIN,
        RpcMethod.INSTALL_PLUGIN,
        RpcMethod.UPDATE_PLUGIN,
        RpcMethod.ADD_MARKETPLACE,
        RpcMethod.REFRESH_MARKETPLACES,
    }
    igni_calls = {
        RpcMethod.GET_MARKETPLACES,
        RpcMethod.ADD_MARKETPLACE,
        RpcMethod.REMOVE_MARKETPLACE,
        RpcMethod.REFRESH_MARKETPLACES,
    }
    assert len(bindings) == 11
    for method, (_, binding) in bindings.items():
        assert binding.duration is (RpcDuration.LONG if method in long_calls else RpcDuration.IO), (
            method
        )
        assert binding.scope is (RpcScope.IGNI if method in igni_calls else RpcScope.SESSION), (
            method
        )
