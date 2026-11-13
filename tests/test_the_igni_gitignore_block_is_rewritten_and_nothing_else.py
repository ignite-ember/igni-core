"""``IgniGitignore``: igni's marked block, and only that block, is igni's.

Pinned directly on the text: what a fresh file gets, how a person's own lines
above and below survive, how a block that lost its end marker is taken back
without eating what the person wrote, and when ``ensure`` says it wrote.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from igni_core.core.init.gitignore import BLOCK_BEGIN, BLOCK_END, BLOCK_LINES, IgniGitignore


def _ig(tmp_path: Path) -> IgniGitignore:
    return IgniGitignore(tmp_path)


class TestBlock:
    def test_it_is_the_markers_around_every_line_and_ends_with_a_newline(self, tmp_path):
        block = _ig(tmp_path).block()

        assert block.splitlines() == [BLOCK_BEGIN, *BLOCK_LINES, BLOCK_END]
        assert block.endswith("\n")

    def test_it_ignores_what_is_personal_and_never_the_shared_files(self):
        active = {line for line in BLOCK_LINES if not line.startswith("#")}

        assert {
            "*.local",
            "*.db",
            "credentials.json",
            "*.log",
            ".checksums.json",
            "*.new",
        } <= active
        assert "group-policy/" in active
        for shared in ("config.yaml", "settings.json", "agents/", "skills/", "hooks/", ".mcp.json"):
            assert shared not in active


class TestRender:
    def test_a_file_without_a_block_gets_one_after_a_blank_line(self, tmp_path):
        out = _ig(tmp_path).render("node_modules/\n")

        assert out == "node_modules/\n\n" + _ig(tmp_path).block()

    def test_a_last_line_without_a_newline_is_finished_first(self, tmp_path):
        out = _ig(tmp_path).render("node_modules/")

        assert out == "node_modules/\n\n" + _ig(tmp_path).block()

    def test_an_empty_file_is_just_the_block(self, tmp_path):
        assert _ig(tmp_path).render("") == _ig(tmp_path).block()

    def test_a_current_file_renders_as_itself(self, tmp_path):
        text = "mine\n\n" + _ig(tmp_path).block() + "after\n"

        assert _ig(tmp_path).render(text) == text

    def test_a_stale_block_is_replaced_and_the_lines_around_it_survive(self, tmp_path):
        stale = f"before\n{BLOCK_BEGIN}\n*.old-rule\n{BLOCK_END}\nafter\n"

        out = _ig(tmp_path).render(stale)

        assert out == "before\n" + _ig(tmp_path).block() + "after\n"
        assert "*.old-rule" not in out

    def test_a_lost_end_marker_takes_back_only_igni_lines_and_keeps_the_persons(self, tmp_path):
        text = f"before\n{BLOCK_BEGIN}\n*.db\ncredentials.json\nmy-own-line\n*.log\n"

        out = _ig(tmp_path).render(text)

        assert out == "before\n" + _ig(tmp_path).block() + "my-own-line\n*.log\n"

    def test_a_lost_end_marker_with_nothing_after_the_begin_marker(self, tmp_path):
        out = _ig(tmp_path).render(f"{BLOCK_BEGIN}\nmine\n")

        assert out == _ig(tmp_path).block() + "mine\n"


class TestEnsure:
    def test_it_writes_the_block_into_a_directory_that_does_not_exist_yet(self, tmp_path):
        ig = IgniGitignore(tmp_path / "new" / ".igni")

        assert ig.ensure() is True

        assert ig.path.read_text() == ig.block()

    def test_it_reports_false_and_changes_nothing_when_the_block_is_current(self, tmp_path):
        ig = _ig(tmp_path)
        ig.ensure()
        before = ig.path.read_bytes()

        assert ig.ensure() is False

        assert ig.path.read_bytes() == before

    def test_it_reports_true_when_a_stale_block_is_brought_up_to_date(self, tmp_path):
        ig = _ig(tmp_path)
        ig.path.write_text(f"{BLOCK_BEGIN}\n*.old\n{BLOCK_END}\n")

        assert ig.ensure() is True

        assert ig.path.read_text() == ig.block()

    def test_a_gitignore_that_cannot_be_read_is_left_alone(self, tmp_path):
        ig = _ig(tmp_path)
        ig.path.mkdir()  # reading a directory raises OSError

        assert ig.ensure() is False

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_a_directory_that_cannot_be_written_reports_false_not_an_error(self, tmp_path):
        config_dir = tmp_path / ".igni"
        config_dir.mkdir()
        config_dir.chmod(0o500)
        try:
            assert IgniGitignore(config_dir).ensure() is False
        finally:
            config_dir.chmod(0o700)
        assert not (config_dir / ".gitignore").exists()
