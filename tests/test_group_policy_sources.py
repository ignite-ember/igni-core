"""Tests for the plugin-source trio on group policy overrides.

Covers two surfaces:

  * :class:`GroupPolicyEntry` — the wire shape. Pydantic
    validation rejects half-filled source specs (ref/subdir without
    URL) so the BE catches the mistake before it ever reaches the
    installer.

  * :class:`PluginInstallCoordinator.install` — runtime behavior.
    With a stub installer, plugin entries with a source route through
    ``installer.install(url, ref=, subdir=)``. Entries without a
    source still write YAML to the legacy path. Failures are logged
    and swallowed so one bad plugin can't take down the rest of
    the pack.

The plugin loader's new ``group-policy-igni`` root is covered in
``test_plugins_loader_root_priority.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from igni_core.core.config.group_policy import plugins as plugins_mod
from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.refresher import PackRefresher, RefreshStatus
from igni_core.core.plugins.git import GitError
from igni_core.core.plugins.loader import PluginLoader

# ── Pydantic surface ──────────────────────────────────────────


class TestGroupPolicyEntrySourceFields:
    """The cross-field validator on GroupPolicyEntry."""

    def test_empty_source_fields_pass(self):
        entry = GroupPolicyEntry(
            kind="plugins",
            entry_name="x",
            content="name: x",
            content_type="yaml",
        )
        assert entry.source_url is None
        assert entry.source_ref is None
        assert entry.source_subdir is None

    def test_full_source_triplet_passes(self):
        entry = GroupPolicyEntry(
            kind="plugins",
            entry_name="x",
            content="name: x",
            content_type="yaml",
            source_url="https://github.com/owner/repo",
            source_ref="main",
            source_subdir="plugins/x",
        )
        assert entry.source_url == "https://github.com/owner/repo"
        assert entry.source_ref == "main"
        assert entry.source_subdir == "plugins/x"

    def test_url_only_passes(self):
        """A bare URL is the minimum: no ref or subdir required."""
        entry = GroupPolicyEntry(
            kind="plugins",
            entry_name="x",
            content="",
            content_type="yaml",
            source_url="https://example.com/repo",
        )
        assert entry.source_url == "https://example.com/repo"
        assert entry.source_ref is None
        assert entry.source_subdir is None

    def test_ref_without_url_is_rejected(self):
        """ref + no url → cross-field validator raises."""
        with pytest.raises(ValidationError) as exc_info:
            GroupPolicyEntry(
                kind="plugins",
                entry_name="x",
                content="",
                content_type="yaml",
                source_ref="main",
            )
        assert "source_url" in str(exc_info.value)

    def test_subdir_without_url_is_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            GroupPolicyEntry(
                kind="plugins",
                entry_name="x",
                content="",
                content_type="yaml",
                source_subdir="plugins/x",
            )
        assert "source_url" in str(exc_info.value)

    def test_ref_and_subdir_without_url_is_rejected(self):
        with pytest.raises(ValidationError):
            GroupPolicyEntry(
                kind="plugins",
                entry_name="x",
                content="",
                content_type="yaml",
                source_ref="main",
                source_subdir="plugins/x",
            )

    def test_empty_string_url_with_ref_is_still_rejected(self):
        """Strict behavior: an empty string is still a value, not
        missing. Stops the FE from sneaking a ref past validation
        by setting URL = "".
        """
        with pytest.raises(ValidationError):
            GroupPolicyEntry(
                kind="plugins",
                entry_name="x",
                content="",
                content_type="yaml",
                source_url="",
                source_ref="main",
            )


# ── Cache materialization ──────────────────────────────────────


class _StubInstaller:
    """Captures install() calls for assertion.

    Implements the two seams the pack uses besides ``install``: it is scoped
    to the pack's directory, and it has no user-tier install to hand over.
    """

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._raises = raises

    def scoped_to(self, plugins_dir: Path) -> _StubInstaller:
        return self

    def release_installed_from(self, url: str) -> list[Path]:
        return []

    def install(
        self,
        url: str,
        *,
        ref: str | None = None,
        subdir: str | None = None,
        directory_name: str | None = None,
    ):
        self.calls.append({"url": url, "ref": ref, "subdir": subdir})
        if self._raises is not None:
            raise self._raises


def _pack_with(overrides: list[GroupPolicyEntry]) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Test",
        fetched_at=datetime.now(timezone.utc),
        entries=overrides,
    )


class TestGroupPolicyCacheMaterializePlugin:
    """Source URL routes to installer; no source routes to YAML fallback."""

    def test_source_url_calls_installer_with_args(self, tmp_path: Path):
        cache_dir = tmp_path / "gp"
        installer = _StubInstaller()
        cache = GroupPolicyCache(GroupPolicyLayout(root=cache_dir), installer=installer)

        pack = _pack_with(
            [
                GroupPolicyEntry(
                    kind="plugins",
                    entry_name="remote-plugin",
                    content="",
                    content_type="yaml",
                    source_url="https://github.com/owner/repo",
                    source_ref="main",
                    source_subdir="plugins/agentic-eval",
                ),
            ]
        )

        cache.materialize(pack)

        assert installer.calls == [
            {
                "url": "https://github.com/owner/repo",
                "ref": "main",
                "subdir": "plugins/agentic-eval",
            }
        ]
        # No legacy YAML written for source-equipped plugins.
        assert not (cache_dir / "plugins" / "remote-plugin.yaml").exists()

    def test_url_only_invokes_installer_without_ref_or_subdir(self, tmp_path: Path):
        installer = _StubInstaller()
        cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "gp"), installer=installer)

        pack = _pack_with(
            [
                GroupPolicyEntry(
                    kind="plugins",
                    entry_name="minimal",
                    content="",
                    content_type="yaml",
                    source_url="https://example.com/r.git",
                ),
            ]
        )

        cache.materialize(pack)

        assert installer.calls == [
            {"url": "https://example.com/r.git", "ref": None, "subdir": None}
        ]

    def test_no_source_writes_legacy_yaml_fallback(self, tmp_path: Path):
        installer = _StubInstaller()
        cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "gp"), installer=installer)

        pack = _pack_with(
            [
                GroupPolicyEntry(
                    kind="plugins",
                    entry_name="legacy",
                    content="name: legacy\n",
                    content_type="yaml",
                ),
            ]
        )

        cache.materialize(pack)

        # Installer is NOT called for entries without a source.
        assert installer.calls == []
        legacy = (tmp_path / "gp" / "plugins" / "legacy.yaml").read_text()
        assert legacy == "name: legacy\n"

    def test_disabled_source_url_skips_install(self, tmp_path: Path):
        """Disabled overrides are filtered out before install — an admin
        can temporarily turn off a plugin without un-installing it on
        disk.
        """
        installer = _StubInstaller()
        cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "gp"), installer=installer)

        pack = _pack_with(
            [
                GroupPolicyEntry(
                    kind="plugins",
                    entry_name="off",
                    content="",
                    content_type="yaml",
                    enabled=False,
                    source_url="https://example.com/r.git",
                ),
            ]
        )

        cache.materialize(pack)
        assert installer.calls == []

    def test_installer_failure_is_swallowed(self, tmp_path: Path):
        """One bad plugin must not abort the rest of the pack."""
        installer = _StubInstaller(
            raises=GitError("network unreachable"),
        )
        cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "gp"), installer=installer)

        pack = _pack_with(
            [
                GroupPolicyEntry(
                    kind="plugins",
                    entry_name="bad",
                    content="",
                    content_type="yaml",
                    source_url="https://example.com/bad.git",
                ),
                GroupPolicyEntry(
                    kind="plugins",
                    entry_name="good",
                    content="",
                    content_type="yaml",
                    source_url="https://example.com/good.git",
                ),
            ]
        )

        # Should not raise — both plugins attempted.
        cache.materialize(pack)
        assert len(installer.calls) == 2

    def test_no_installer_configured_logs_warning_no_crash(self, tmp_path: Path):
        """A cache built without an installer for a source-URL row
        shouldn't crash — the row will simply be skipped. Real
        callers wire an installer; this catches dev misuse.

        We patch the module logger directly instead of using
        ``caplog``. Other tests in this repo reconfigure logging
        at runtime, which makes ``caplog`` unreliable when the
        full suite runs (mirrors the rationale in
        ``test_codeindex_filters.py``).
        """
        cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "gp"), installer=None)

        pack = _pack_with(
            [
                GroupPolicyEntry(
                    kind="plugins",
                    entry_name="stranded",
                    content="",
                    content_type="yaml",
                    source_url="https://example.com/r.git",
                ),
            ]
        )

        with patch.object(plugins_mod.logger, "warning") as mock_warning:
            cache.materialize(pack)

        assert mock_warning.called, "expected a warning about missing installer"
        messages = [
            (args[0] % args[1:]) if len(args) > 1 else args[0]
            for args, _ in mock_warning.call_args_list
        ]
        joined = " ".join(messages).lower()
        assert "no installer" in joined
        assert "stranded" in joined

    def test_other_kinds_unaffected(self, tmp_path: Path):
        """Agents/MCPs/settings still write to their normal paths even
        when ``installer`` is wired. The cache only forks on
        kind=plugins.
        """
        installer = _StubInstaller()
        cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "gp"), installer=installer)

        pack = _pack_with(
            [
                GroupPolicyEntry(
                    kind="agents",
                    entry_name="reviewer",
                    content="---\nname: reviewer\n---\n# body",
                    content_type="markdown",
                ),
                GroupPolicyEntry(
                    kind="mcps",
                    entry_name="gh",
                    content='{"mcpServers": {}}',
                    content_type="json",
                ),
            ]
        )

        cache.materialize(pack)

        assert installer.calls == []  # agents/mcps don't install
        assert (tmp_path / "gp" / "agents" / "reviewer.md").exists()
        assert (tmp_path / "gp" / "mcps" / "gh.json").exists()


# ── Plugin loader: new root ────────────────────────────────────


class TestPluginLoaderGroupPolicyRoot:
    """The new ``group-policy-igni`` root at priority 5."""

    @staticmethod
    def _capture_roots(tmp_path: Path) -> dict[str, tuple[Path, int]]:
        """Run load_all and return the (path, priority) tuple per root."""
        loader = PluginLoader(data_dir=tmp_path)
        captured: list[tuple[str, Path, int]] = []
        loader._load_root = lambda k, rp, prio: captured.append((k, rp, prio))
        loader.load_all(tmp_path)
        return {k: (rp, prio) for k, rp, prio in captured}

    def test_root_registered_with_data_dir_relative_path(self, tmp_path: Path):
        roots = self._capture_roots(tmp_path)
        assert "group-policy-igni" in roots
        path, priority = roots["group-policy-igni"]
        assert path == tmp_path / "group-policy" / "plugins"
        assert priority == 5

    def test_priority_outranks_user_and_project_tiers(self, tmp_path: Path):
        roots = self._capture_roots(tmp_path)
        prios = {k: v[1] for k, v in roots.items()}

        # Outrank user + project
        assert prios["group-policy-igni"] > prios["user-igni"]
        assert prios["group-policy-igni"] > prios["project-igni"]
        assert prios["group-policy-igni"] > prios["project-claude"]

    def test_priority_loses_to_managed_tiers(self, tmp_path: Path):
        roots = self._capture_roots(tmp_path)
        prios = {k: v[1] for k, v in roots.items()}

        # Submit to the OS-managed tiers
        assert prios["group-policy-igni"] < prios["managed-claude"]
        assert prios["group-policy-igni"] < prios["managed-igni"]

    def test_loader_does_not_flag_group_policy_as_managed(self, tmp_path: Path):
        """Group-policy installs aren't OS-managed — users can still
        disable them via the existing ``plugin_disabled_names`` flow.
        """
        # Lay down a stub plugin under the new root.
        plugin_root = tmp_path / "group-policy" / "plugins" / "test-plugin"
        plugin_root.mkdir(parents=True)
        manifest_dir = plugin_root / ".claude-plugin"
        manifest_dir.mkdir()
        (manifest_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "test-plugin",
                    "version": "1.0.0",
                    "description": "Test plugin",
                }
            )
        )

        loader = PluginLoader(data_dir=tmp_path)
        loader.load_all(tmp_path)

        plugin = loader.get("test-plugin")
        assert plugin is not None
        # Root kind, not priority, drives is_managed — and the
        # new tier isn't in the managed-claude/managed-igni set.
        assert plugin.is_managed is False


# ---------------------------------------------------------------------------
# Cache hydration (PackRefresher.refresh_if_stale)
# ---------------------------------------------------------------------------


def _make_pack(*, fetched_at: datetime) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-test",
        group_name="Engineering",
        fetched_at=fetched_at,
        entries=[
            GroupPolicyEntry(
                kind="agents",
                entry_name="code-reviewer",
                content="---\nname: code-reviewer\n---\nA reviewer.",
                content_type="markdown",
                enabled=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_refresh_if_stale_skips_when_cache_is_fresh(tmp_path: Path):
    """A 60-second-old cache does NOT trigger a fetch."""
    cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path))
    cache.materialize(_make_pack(fetched_at=datetime.now(timezone.utc).replace(microsecond=0)))

    fetch_called = False

    async def _fetch(token: str, etag: str | None):
        nonlocal fetch_called
        fetch_called = True
        return None

    result = await PackRefresher(cache).refresh_if_stale("t-1", _fetch)
    assert result.status is RefreshStatus.fresh
    assert fetch_called is False


@pytest.mark.asyncio
async def test_refresh_if_stale_fetches_when_meta_missing(tmp_path: Path):
    """Empty cache directory → fetch + materialize."""
    cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path))
    pack = _make_pack(fetched_at=datetime.now(timezone.utc))

    async def _fetch(token: str, etag: str | None):
        assert token == "t-2"
        return pack

    result = await PackRefresher(cache).refresh_if_stale("t-2", _fetch)
    assert result.ok
    # Cache now populated
    meta = cache.meta.load()
    assert meta is not None
    assert meta.group_id == "g-test"
    # And the agent file is on disk
    assert (cache.agents_dir / "code-reviewer.md").exists()


@pytest.mark.asyncio
async def test_refresh_if_stale_fetches_when_cache_is_stale(tmp_path: Path):
    """A 600-second-old cache (> 5 min TTL) DOES trigger a fetch."""
    cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path))
    old = datetime.now(timezone.utc) - timedelta(seconds=600)
    cache.materialize(_make_pack(fetched_at=old))

    new_pack = _make_pack(fetched_at=datetime.now(timezone.utc).replace(microsecond=0))

    async def _fetch(token: str, etag: str | None):
        return new_pack

    result = await PackRefresher(cache).refresh_if_stale("t-3", _fetch)
    assert result.ok
    # Pack meta must be re-stamped with the fresh fetched_at
    meta = cache.meta.load()
    assert meta is not None
    assert meta.fetched_at > old


@pytest.mark.asyncio
async def test_refresh_if_stale_returns_false_when_fetch_returns_none(
    tmp_path: Path,
):
    """Network/server failure → no disk write, no crash."""
    cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path))

    async def _fetch(token: str, etag: str | None):
        return None

    result = await PackRefresher(cache).refresh_if_stale("t-4", _fetch)
    # Pack was None — disk untouched, no update.
    assert result.status is RefreshStatus.no_answer
    assert cache.meta.load() is None


@pytest.mark.asyncio
async def test_refresh_if_stale_treats_empty_token_as_noop(tmp_path: Path):
    """An empty bearer must not call fetch."""
    cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path))

    async def _fetch(token: str, etag: str | None):
        raise AssertionError("fetch should not be called")

    result = await PackRefresher(cache).refresh_if_stale("", _fetch)
    assert result.status is RefreshStatus.no_token


@pytest.mark.asyncio
async def test_a_cache_for_a_data_dir_lands_under_it(tmp_path: Path):
    """``for_data_dir`` derives ``<data_dir>/group-policy`` — the one derivation."""
    pack = _make_pack(fetched_at=datetime.now(timezone.utc))

    async def _fetch(token: str, etag: str | None):
        return pack

    cache = GroupPolicyCache.for_data_dir(tmp_path)
    assert (await PackRefresher(cache).refresh_if_stale("t-5", _fetch)).ok
    on_disk = tmp_path / "group-policy" / "agents" / "code-reviewer.md"
    assert on_disk.exists()
