"""What one materialise pass writes, skips, prunes and records.

The pack's entries are the organisation's policy; the materialiser is the
only thing that turns them into files the loaders read. These tests drive
it directly over a real directory.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.kinds import KindRegistry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.materializer import EntryMaterializer, MaterializeResult
from igni_core.core.config.group_policy.meta import PackMetaStore
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.plugins import PluginInstallCoordinator


def _entry(kind: str, name: str, content: str = "body", **extra) -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind=kind,
        entry_name=name,
        content=content,
        content_type=extra.pop("content_type", "markdown"),
        **extra,
    )


def _pack(*entries: GroupPolicyEntry) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        entries=list(entries),
    )


@pytest.fixture
def layout(tmp_path: Path) -> GroupPolicyLayout:
    return GroupPolicyLayout(root=tmp_path / "group-policy")


@pytest.fixture
def materializer(layout: GroupPolicyLayout) -> EntryMaterializer:
    plugins = PluginInstallCoordinator(layout)
    return EntryMaterializer(layout, KindRegistry.default(plugins), plugins, PackMetaStore(layout))


class TestWhatIsWritten:
    def test_an_enabled_entry_lands_in_its_kinds_directory(self, materializer, layout):
        result = materializer.materialize(_pack(_entry("agents", "reviewer")))

        assert (layout.dir_for("agents") / "reviewer.md").read_text() == "body"
        assert result == MaterializeResult(skipped=[])

    def test_a_disabled_entry_writes_nothing(self, materializer, layout):
        result = materializer.materialize(_pack(_entry("agents", "reviewer", enabled=False)))

        assert not (layout.dir_for("agents") / "reviewer.md").exists()
        assert result.skipped == []

    def test_an_entry_of_a_kind_nobody_knows_is_ignored_and_the_rest_apply(
        self, materializer, layout
    ):
        result = materializer.materialize(
            _pack(_entry("telepathy", "x"), _entry("agents", "reviewer"))
        )

        assert (layout.dir_for("agents") / "reviewer.md").is_file()
        assert not layout.dir_for("telepathy").exists()
        assert result.skipped == []

    def test_a_settings_entry_is_never_written_to_disk(self, materializer, layout):
        materializer.materialize(
            _pack(_entry("settings", "tuning", '{"learning": {}}', content_type="json"))
        )

        assert not layout.dir_for("settings").exists()
        assert not [p for p in layout.root.rglob("tuning*")]

    def test_an_empty_pack_writes_the_directories_and_a_zero_count_meta(self, materializer, layout):
        result = materializer.materialize(_pack())

        assert result.skipped == []
        assert layout.dir_for("scripts").is_dir()
        assert layout.dir_for("hooks").is_dir()
        assert not layout.hooks_file.exists()
        stored = json.loads(layout.file(GroupPolicyLayout.META_FILE).read_text())
        assert stored["entry_count"] == 0
        assert stored["group_id"] == "g-1"

    def test_the_hooks_are_written_with_the_scripts_directory_filled_in(self, materializer, layout):
        hook = _entry(
            "hooks",
            "audit",
            json.dumps({"event": "PreToolUse", "command": "{group_scripts}/audit.sh"}),
            content_type="json",
        )

        materializer.materialize(_pack(hook))

        written = json.loads(layout.hooks_file.read_text())
        command = written["hooks"]["PreToolUse"][0]["command"]
        assert command == f"{layout.dir_for('scripts')}/audit.sh"

    def test_the_meta_counts_every_entry_the_pack_carries(self, materializer, layout):
        materializer.materialize(
            _pack(_entry("agents", "a"), _entry("agents", "b", enabled=False), _entry("zzz", "c"))
        )

        stored = json.loads(layout.file(GroupPolicyLayout.META_FILE).read_text())
        assert stored["entry_count"] == 3


class TestAnUnsafeNameCostsOnlyItself:
    @pytest.mark.parametrize("name", ["../../escape", "a/b", "..", "~root", "", "C:x", "nul\x00"])
    def test_it_is_skipped_named_and_warned_about(
        self, materializer, layout, caplog, name, tmp_path
    ):
        caplog.set_level(logging.WARNING)
        pack = _pack(_entry("agents", name), _entry("agents", "good"))

        result = materializer.materialize(pack)

        assert result.skipped == [name]
        assert (layout.dir_for("agents") / "good.md").is_file()
        assert sorted(p.name for p in layout.dir_for("agents").iterdir()) == ["good.md"]
        assert not (tmp_path / "escape.md").exists()
        warning = next(r for r in caplog.records if "entry skipped" in r.getMessage())
        assert "agents entry cannot be written under that name" in warning.getMessage()
        assert "the rest of the pack was applied" in warning.getMessage()

    def test_several_are_all_named_in_order(self, materializer):
        result = materializer.materialize(
            _pack(_entry("agents", "a/b"), _entry("skills", ".."), _entry("agents", "ok"))
        )

        assert result.skipped == ["a/b", ".."]

    def test_a_disabled_unsafe_entry_is_not_reported(self, materializer):
        result = materializer.materialize(_pack(_entry("agents", "a/b", enabled=False)))

        assert result.skipped == []


class TestWhatThePackNoLongerShipsGoes:
    def test_a_file_the_pack_dropped_is_removed_and_a_kept_one_stays(self, materializer, layout):
        materializer.materialize(_pack(_entry("agents", "old"), _entry("agents", "kept")))

        materializer.materialize(_pack(_entry("agents", "kept")))

        assert sorted(p.name for p in layout.dir_for("agents").iterdir()) == ["kept.md"]

    def test_a_skill_directory_the_pack_dropped_is_removed_whole(self, materializer, layout):
        materializer.materialize(_pack(_entry("skills", "deploy")))
        assert (layout.dir_for("skills") / "deploy" / "SKILL.md").is_file()

        materializer.materialize(_pack())

        assert list(layout.dir_for("skills").iterdir()) == []

    def test_a_file_added_beside_the_shipped_ones_is_removed_on_a_rebuild(
        self, materializer, layout
    ):
        materializer.materialize(_pack(_entry("agents", "a")))
        (layout.dir_for("agents") / "planted.md").write_text("evil")

        materializer.materialize(_pack(_entry("agents", "a")), rebuild=True)

        assert sorted(p.name for p in layout.dir_for("agents").iterdir()) == ["a.md"]

    def test_an_edited_file_is_overwritten_with_the_packs_text(self, materializer, layout):
        materializer.materialize(_pack(_entry("agents", "a", "signed")))
        (layout.dir_for("agents") / "a.md").write_text("edited")

        materializer.materialize(_pack(_entry("agents", "a", "signed")), rebuild=True)

        assert (layout.dir_for("agents") / "a.md").read_text() == "signed"

    def test_a_plugin_directory_the_pack_does_not_name_is_removed(self, materializer, layout):
        planted = layout.dir_for("plugins") / "planted"
        planted.mkdir(parents=True)
        (planted / "plugin.json").write_text("{}")

        materializer.materialize(_pack())

        assert not planted.exists()

    @pytest.mark.skipif(os.name == "nt", reason="needs POSIX permissions")
    def test_a_path_that_cannot_be_removed_does_not_fail_the_pass(
        self, materializer, layout, monkeypatch
    ):
        materializer.materialize(_pack(_entry("agents", "a"), _entry("agents", "stale")))
        stale = layout.dir_for("agents") / "stale.md"
        real_unlink = Path.unlink

        def refuse(self, *args, **kwargs):
            if self == stale:
                raise PermissionError("busy")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", refuse)

        result = materializer.materialize(_pack(_entry("agents", "a")))

        assert stale.exists()
        assert (layout.dir_for("agents") / "a.md").is_file()
        assert result.skipped == []
        assert json.loads(layout.file(GroupPolicyLayout.META_FILE).read_text())["entry_count"] == 1


class TestClearDerived:
    def _cache(self, tmp_path: Path) -> GroupPolicyCache:
        return GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "group-policy"))

    def test_every_kind_directory_goes_and_the_signed_material_stays(self, tmp_path):
        cache = self._cache(tmp_path)
        cache.materialize(
            _pack(
                _entry("agents", "a"),
                _entry("skills", "s"),
                _entry("hooks", "h", json.dumps({"event": "Stop", "command": "x"})),
            )
        )
        root = cache.layout.root
        for name in ("pack.jws", "jwks.json", "pack_state.json"):
            (root / name).write_text(name)

        cache.clear_derived()

        assert sorted(p.name for p in root.iterdir()) == [
            "jwks.json",
            "pack.jws",
            "pack_meta.json",
            "pack_state.json",
        ]

    def test_a_directory_that_is_not_there_is_fine(self, tmp_path):
        cache = self._cache(tmp_path)

        cache.clear_derived()

        assert not cache.layout.root.exists()

    @pytest.mark.skipif(os.name == "nt", reason="needs POSIX permissions")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_a_directory_that_cannot_be_emptied_does_not_stop_the_others(self, tmp_path):
        cache = self._cache(tmp_path)
        cache.materialize(_pack(_entry("agents", "a"), _entry("commands", "c")))
        agents = cache.layout.dir_for("agents")
        agents.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            cache.clear_derived()
        finally:
            agents.chmod(stat.S_IRWXU)

        assert not cache.layout.dir_for("commands").exists()
