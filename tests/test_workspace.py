"""Tests for workspace.py — multi-directory workspace support."""

import pytest

from igni_core.core.workspace import WorkspaceManager


class TestWorkspaceManager:
    def test_single_dir(self, tmp_path):
        ws = WorkspaceManager(tmp_path)
        assert ws.all_dirs == [tmp_path.resolve()]
        assert not ws.is_multi
        assert ws.get_context_instructions() == ""
        assert ws.short_label() == ""

    def test_multi_dir(self, tmp_path):
        extra1 = tmp_path / "extra1"
        extra2 = tmp_path / "extra2"
        extra1.mkdir()
        extra2.mkdir()

        ws = WorkspaceManager(tmp_path, [extra1, extra2])
        assert len(ws.all_dirs) == 3
        assert ws.is_multi
        assert ws.short_label() == "+2 dirs"

    def test_single_extra_label(self, tmp_path):
        extra = tmp_path / "extra"
        extra.mkdir()

        ws = WorkspaceManager(tmp_path, [extra])
        assert ws.short_label() == "+1 dir"

    def test_deduplicates_primary(self, tmp_path):
        ws = WorkspaceManager(tmp_path, [tmp_path])
        assert len(ws.all_dirs) == 1
        assert not ws.is_multi

    def test_context_instructions_contain_paths(self, tmp_path):
        extra = tmp_path / "other-project"
        extra.mkdir()

        ws = WorkspaceManager(tmp_path, [extra])
        ctx = ws.get_context_instructions()
        assert "Primary" in ctx
        assert "Additional" in ctx
        assert str(extra.resolve()) in ctx
        assert "absolute paths" in ctx

    def test_raises_on_missing_dir(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        with pytest.raises(ValueError, match="does not exist"):
            WorkspaceManager(tmp_path, [missing])

    def test_a_file_is_refused_as_an_additional_dir(self, tmp_path):
        a_file = tmp_path / "notes.txt"
        a_file.write_text("x")
        with pytest.raises(ValueError, match="Additional directory does not exist"):
            WorkspaceManager(tmp_path, [a_file])

    def test_an_alias_of_the_primary_is_dropped(self, tmp_path):
        (tmp_path / "sub").mkdir()
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        ws = WorkspaceManager(tmp_path, [link, tmp_path / "sub" / ".."])
        assert ws.all_dirs == [tmp_path.resolve()]
        assert not ws.is_multi

    def test_the_same_extra_dir_twice_is_kept_twice(self, tmp_path):
        extra = tmp_path / "extra"
        extra.mkdir()
        ws = WorkspaceManager(tmp_path, [extra, extra])
        assert ws.additional_dirs == [extra.resolve(), extra.resolve()]
        assert ws.short_label() == "+2 dirs"

    def test_dirs_keep_the_given_order_primary_first(self, tmp_path):
        a = tmp_path / "b-first"
        b = tmp_path / "a-second"
        a.mkdir()
        b.mkdir()
        ws = WorkspaceManager(tmp_path, [a, b])
        assert ws.all_dirs == [tmp_path.resolve(), a.resolve(), b.resolve()]

    def test_none_and_empty_additional_dirs_mean_single(self, tmp_path):
        for extras in (None, []):
            ws = WorkspaceManager(tmp_path, extras)
            assert ws.additional_dirs == []
            assert ws.get_context_instructions() == ""
            assert ws.short_label() == ""

    def test_instructions_number_the_extras_from_one(self, tmp_path):
        extras = []
        for name in ("one dir", "two-é", "three"):
            d = tmp_path / name
            d.mkdir()
            extras.append(d)
        ws = WorkspaceManager(tmp_path, extras)
        lines = ws.get_context_instructions().split("\n")
        assert lines[0] == "## Active Workspaces"
        assert lines[2] == (
            "You have access to multiple project directories. Use absolute paths when referencing files."
        )
        assert lines[4] == f"**Primary:** `{tmp_path.resolve()}`"
        assert lines[5:8] == [
            f"**Additional [1]:** `{extras[0].resolve()}`",
            f"**Additional [2]:** `{extras[1].resolve()}`",
            f"**Additional [3]:** `{extras[2].resolve()}`",
        ]
        assert lines[8] == ""
        assert lines[9] == (
            "When searching or reading files, specify which workspace you're operating in. "
            "Tools accept absolute paths and will work across all workspaces."
        )
        assert len(lines) == 10
        assert ws.short_label() == "+3 dirs"

    def test_a_missing_primary_is_not_validated(self, tmp_path):
        ws = WorkspaceManager(tmp_path / "nope")
        assert ws.all_dirs == [(tmp_path / "nope").resolve()]
