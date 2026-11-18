"""Tests for the managed plugin scope (row 35) — 4th install
tier matching CC's managed-policy plugins.

Covers:
- Platform path lookup (darwin/linux/win32/unknown).
- Loader discovers plugins from the managed root with priority 5/6.
- ``PluginDefinition.is_managed`` is True for plugins from
  managed roots, False for everything else.
- Same-name collisions: managed beats project beats user.
- ``set_plugin_enabled(..., enabled=False)`` refuses to disable
  a managed plugin (RPC-level enforcement).
- Session-level enforcement: a managed plugin in the persisted
  disable list is silently ignored (the disable-set strips it).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from igni_core.backend.command_handler import CommandHandler
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.plugins import state as state_mod
from igni_core.core.plugins.loader import PluginLoader, _platform_managed_plugins_root
from igni_core.core.plugins.models import (
    PluginDefinition,
    PluginInfo,
    PluginManifest,
    PluginSource,
)
from igni_core.core.session.core import PluginReloadCounts
from tests.backend_server_factory import make_backend

# Alias for the deleted ``settings._platform_managed_settings_path``
# shim — kept as a local name so the assertion site below reads
# naturally after the OOP-refactor moved the discovery onto
# :class:`ManagedPolicySource`.
_platform_managed_settings_path = ManagedPolicySource.platform_path


def _make_plugin(root: Path, name: str, version: str = "0.1.0") -> None:
    """Write a minimal Claude-Code-shaped plugin under ``root/<name>/``."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version})
    )


# ── Platform path ────────────────────────────────────────────


class TestPlatformManagedPluginsRoot:
    def test_darwin(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        path = _platform_managed_plugins_root()
        assert path is not None
        assert str(path) == "/Library/Application Support/igni"

    def test_linux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        path = _platform_managed_plugins_root()
        assert path is not None
        assert str(path) == "/etc/igni"

    def test_win32(self, monkeypatch):
        monkeypatch.setenv("PROGRAMDATA", r"C:\TestProgramData")
        monkeypatch.setattr("sys.platform", "win32")
        path = _platform_managed_plugins_root()
        assert path is not None
        assert "igni" in str(path)

    def test_unknown(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "freebsd")
        assert _platform_managed_plugins_root() is None

    def test_shared_root_with_managed_settings(self):
        """The managed plugin root IS the same parent as the
        managed-settings file. A sysadmin drops settings,
        instructions, and plugins under one OS-protected
        directory."""
        plugin_root = _platform_managed_plugins_root()
        settings_path = _platform_managed_settings_path()
        if plugin_root is None:
            assert settings_path is None
        else:
            assert settings_path is not None
            assert settings_path.parent == plugin_root


# ── Loader discovery ─────────────────────────────────────────


class TestManagedDiscovery:
    def test_managed_root_loaded(self, tmp_path, monkeypatch):
        """A plugin under ``<managed>/.igni/plugins/`` is
        discovered with ``source.root == "managed-igni"`` and
        ``is_managed == True``."""
        managed = tmp_path / "managed"
        _make_plugin(managed / CONFIG_DIR / "plugins", "org-policy")
        monkeypatch.setattr(
            "igni_core.core.plugins.loader._platform_managed_plugins_root",
            lambda: managed,
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        loader = PluginLoader()
        loader.load_all(project_dir=tmp_path / "project")

        plugin = loader.get("org-policy")
        assert plugin is not None
        assert plugin.source.root == "managed-igni"
        assert plugin.is_managed is True

    def test_managed_claude_namespace(self, tmp_path, monkeypatch):
        """The ``.claude/plugins`` subtree under the managed root
        is also scanned — for cross-tool plugins that ship under
        CC's namespace but need org enforcement."""
        managed = tmp_path / "managed"
        _make_plugin(managed / ".claude" / "plugins", "cross-tool-policy")
        monkeypatch.setattr(
            "igni_core.core.plugins.loader._platform_managed_plugins_root",
            lambda: managed,
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        loader = PluginLoader()
        loader.load_all(project_dir=tmp_path / "project")

        plugin = loader.get("cross-tool-policy")
        assert plugin is not None
        assert plugin.source.root == "managed-claude"
        assert plugin.is_managed is True

    def test_managed_beats_project_on_collision(self, tmp_path, monkeypatch):
        """Highest priority wins same-name discovery. Managed
        plugin SHADOWS a project plugin of the same name."""
        managed = tmp_path / "managed"
        project = tmp_path / "project"
        _make_plugin(managed / CONFIG_DIR / "plugins", "shared", version="9.9.9")
        _make_plugin(project / CONFIG_DIR / "plugins", "shared", version="0.1.0")
        monkeypatch.setattr(
            "igni_core.core.plugins.loader._platform_managed_plugins_root",
            lambda: managed,
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        loader = PluginLoader()
        loader.load_all(project_dir=project)
        plugin = loader.get("shared")
        assert plugin is not None
        assert plugin.is_managed is True
        assert plugin.manifest.version == "9.9.9"

    def test_no_managed_dir_skipped(self, tmp_path, monkeypatch):
        """If the platform returns ``None`` (unknown OS) the
        loader skips the managed tier without crashing."""
        monkeypatch.setattr(
            "igni_core.core.plugins.loader._platform_managed_plugins_root",
            lambda: None,
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        _make_plugin(tmp_path / "project" / CONFIG_DIR / "plugins", "p")
        loader = PluginLoader()
        loader.load_all(project_dir=tmp_path / "project")
        plugin = loader.get("p")
        assert plugin is not None
        assert plugin.is_managed is False

    def test_non_managed_plugins_keep_is_managed_false(self, tmp_path, monkeypatch):
        """Sanity: every non-managed source must report
        ``is_managed == False``. Catches regressions if a future
        refactor accidentally widens the managed-detection
        predicate."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        _make_plugin(tmp_path / "home" / ".claude" / "plugins", "uc")
        _make_plugin(tmp_path / "home" / CONFIG_DIR / "plugins", "ue")
        _make_plugin(tmp_path / "project" / ".claude" / "plugins", "pc")
        _make_plugin(tmp_path / "project" / CONFIG_DIR / "plugins", "pe")
        monkeypatch.setattr(
            "igni_core.core.plugins.loader._platform_managed_plugins_root",
            lambda: None,
        )
        loader = PluginLoader()
        loader.load_all(project_dir=tmp_path / "project")
        for name in ("uc", "ue", "pc", "pe"):
            plugin = loader.get(name)
            assert plugin is not None, name
            assert plugin.is_managed is False, name


# ── PluginInfo wire shape ────────────────────────────────────


class TestPluginInfo:
    def test_managed_field_exposed(self):
        """PluginInfo (the panel's wire shape) carries the
        ``managed`` flag so the UI can lock the disable toggle."""
        info = PluginInfo(name="x", managed=True)
        assert info.managed is True

    def test_default_managed_is_false(self):
        info = PluginInfo(name="x")
        assert info.managed is False


# ── set_plugin_enabled refuses to disable managed ────────────


class TestSetPluginEnabledRefusesManaged:
    def test_disable_managed_returns_explanatory_error(self, tmp_path, monkeypatch):
        """The RPC must refuse to mark a managed plugin disabled —
        a sysadmin policy isn't optional."""
        managed_plugin = PluginDefinition(
            manifest=PluginManifest(name="org-policy"),
            source=PluginSource(
                root="managed-igni",
                path=tmp_path / "managed" / CONFIG_DIR / "plugins" / "org-policy",
                priority=6,
            ),
        )

        session = MagicMock()
        session.plugin_loader.get.return_value = managed_plugin
        session.plugin_state.disabled = []
        backend = make_backend(session)

        result = backend.controllers.plugins.set_enabled("org-policy", enabled=False)
        # The error text should mention "managed" so the user
        # understands WHY the toggle didn't take effect.
        assert "managed" in result.text.lower()
        # And the session reload path must NOT have fired — the
        # disable was refused before any state mutation.
        session.reload_plugins.assert_not_called()

    def test_enable_managed_does_not_hit_refusal_path(self, tmp_path, monkeypatch):
        """Enabling a managed plugin should NOT trip the refusal
        path that disable hits. Stubbed save/reload so the test
        only exercises the branch logic in ``set_plugin_enabled``."""
        managed_plugin = PluginDefinition(
            manifest=PluginManifest(name="org-policy"),
            source=PluginSource(
                root="managed-igni",
                path=tmp_path / "managed",
                priority=6,
            ),
        )
        session = MagicMock()
        session.plugin_loader.get.return_value = managed_plugin
        session.plugin_state.disabled = []
        session.settings.storage.data_dir = str(tmp_path / "data")
        backend = make_backend(session)

        # The hot-reload + state-save aren't what we're testing
        # here — neutralise both so the MagicMock state object
        # doesn't get serialised.
        monkeypatch.setattr(state_mod, "save_state", lambda *a, **kw: None)
        result = backend.controllers.plugins.set_enabled("org-policy", enabled=True)
        # No managed-refusal text in the reply.
        assert "cannot be disabled" not in result.text.lower()
        assert "enabled" in result.text.lower()


# ── the slash command refuses to disable a managed plugin ─────────


class TestSlashDisableRefusesManaged:
    """``/plugins disable`` must refuse a managed plugin, the way the
    panel's RPC does.

    The RPC refuses explicitly, so the person learns why. The slash
    command used to write the name into the disabled list and answer
    "disabled" — and since the session drops managed plugins from the
    disabled set, the plugin stayed on and the answer was a lie. It
    also left a stale entry behind: should the managed directory ever
    go away, that entry would disable the plugin for good.
    """

    def _handler(self, tmp_path: Path, monkeypatch) -> CommandHandler:
        managed = tmp_path / "managed"
        _make_plugin(managed / CONFIG_DIR / "plugins", "org-policy")

        loader = PluginLoader()
        monkeypatch.setattr(
            "igni_core.core.plugins.loader._platform_managed_plugins_root",
            lambda: managed,
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        loader.load_all(project_dir=tmp_path / "proj")

        session = MagicMock()
        session.plugin_loader = loader
        session.plugin_state = state_mod.PluginsState(disabled=[])
        session.settings.storage.data_dir = str(tmp_path / "data")
        session.plugin_data_dir = str(tmp_path / "data")
        session.reload_plugins.return_value = PluginReloadCounts(
            plugins=0, skills=0, agents=0, hooks=0
        )
        return CommandHandler(session)

    def test_the_command_refuses_and_says_why(self, tmp_path: Path, monkeypatch) -> None:
        """The answer names the plugin and says it is managed."""
        h = self._handler(tmp_path, monkeypatch)
        result = asyncio.run(h.handle("/plugins disable org-policy"))
        assert result.kind == "info"
        assert "managed" in result.content.lower()
        assert "cannot be disabled" in result.content.lower()

    def test_nothing_is_written_to_the_state(self, tmp_path: Path, monkeypatch) -> None:
        """No disabled entry, and the session was not reloaded — the
        refusal happens before any state is touched."""
        h = self._handler(tmp_path, monkeypatch)
        asyncio.run(h.handle("/plugins disable org-policy"))
        persisted = state_mod.load_state(data_dir=tmp_path / "data")
        assert persisted.disabled == []
        h._session.reload_plugins.assert_not_called()  # type: ignore[attr-defined]
