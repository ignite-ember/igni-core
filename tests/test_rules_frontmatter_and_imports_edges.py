"""Edges of the rules files' frontmatter parser and ``@`` import resolver
that the neighbouring tests leave open."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from igni_core.core.utils.context_frontmatter import matches_paths, parse_frontmatter
from igni_core.core.utils.context_imports import (
    IMPORT_MAX_DEPTH,
    resolve_at_path,
    resolve_imports,
)


class TestFrontmatterEdges:
    def test_windows_line_endings_are_read_like_unix_ones(self):
        content = "---\r\npaths:\r\n  - docs/**\r\n  - 'tests/**'\r\n---\r\nbody\r\nmore"
        paths, body = parse_frontmatter(content)
        assert paths == ["docs/**", "tests/**"]
        assert body == "body\r\nmore"

    def test_an_unterminated_fence_is_no_frontmatter_and_the_text_is_the_body(self):
        content = "---\npaths: [docs/**]\nbody that never closes"
        assert parse_frontmatter(content) == ([], content)

    def test_a_fence_that_is_not_first_is_no_frontmatter(self):
        content = "intro\n---\npaths: [docs/**]\n---\nbody"
        assert parse_frontmatter(content) == ([], content)

    def test_an_empty_body_after_the_closing_fence_is_empty(self):
        assert parse_frontmatter("---\npaths: [a]\n---\n") == (["a"], "")
        assert parse_frontmatter("---\npaths: [a]\n---") == (["a"], "")

    def test_a_key_that_only_mentions_paths_in_its_value_is_not_paths(self):
        paths, _ = parse_frontmatter("---\ndescription: the paths: here\n---\nbody")
        assert paths == []

    def test_blank_inline_items_are_dropped(self):
        paths, _ = parse_frontmatter('---\npaths: [a, , "b", ""]\n---\nx')
        assert paths == ["a", "b"]

    def test_a_working_dir_that_is_a_symlink_is_matched_by_its_real_location(self, tmp_path: Path):
        real = tmp_path / "project" / "docs"
        real.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real)
        assert matches_paths(["docs"], link, tmp_path / "project")


class TestImportEdges:
    def test_a_symlink_that_leaves_the_root_is_not_followed(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET")
        (root / "link.md").symlink_to(outside)
        source = root / "igni.md"
        assert resolve_at_path("link.md", source, root) is None
        assert resolve_imports("see @link.md", source, root) == "see @link.md"

    def test_a_file_that_is_not_utf8_is_left_as_its_literal_token(self, tmp_path: Path):
        (tmp_path / "bad.md").write_bytes(b"\xff\xfe\x00 not text")
        source = tmp_path / "igni.md"
        assert resolve_imports("a @bad.md b", source, tmp_path) == "a @bad.md b"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_an_unreadable_file_is_left_as_its_literal_token(self, tmp_path: Path):
        (tmp_path / "locked.md").write_text("hidden")
        (tmp_path / "locked.md").chmod(0)
        try:
            out = resolve_imports("@locked.md", tmp_path / "igni.md", tmp_path)
        finally:
            (tmp_path / "locked.md").chmod(0o644)
        assert out == "@locked.md"

    def test_a_directory_named_like_a_markdown_file_is_left_literal(self, tmp_path: Path):
        (tmp_path / "dir.md").mkdir()
        assert resolve_imports("@dir.md", tmp_path / "igni.md", tmp_path) == "@dir.md"

    def test_a_file_imported_twice_in_one_file_is_inlined_only_the_first_time(self, tmp_path: Path):
        (tmp_path / "shared.md").write_text("SHARED")
        out = resolve_imports("one @shared.md two @shared.md", tmp_path / "igni.md", tmp_path)
        assert out == "one SHARED two @shared.md"

    def test_two_different_files_are_both_inlined(self, tmp_path: Path):
        (tmp_path / "a.md").write_text("A")
        (tmp_path / "b.md").write_text("B")
        assert resolve_imports("@a.md @b.md", tmp_path / "igni.md", tmp_path) == "A B"

    def test_a_markdown_link_is_not_an_import(self, tmp_path: Path):
        (tmp_path / "foo.md").write_text("FOO")
        text = "[label](./foo.md) and (@foo.md)"
        assert (
            resolve_imports(text, tmp_path / "igni.md", tmp_path) == "[label](./foo.md) and (FOO)"
        )

    def test_an_email_like_token_that_is_not_a_file_is_left_alone(self, tmp_path: Path):
        text = "mail me@example.md now"
        assert resolve_imports(text, tmp_path / "igni.md", tmp_path) == text

    def test_the_chain_stops_after_the_depth_cap(self, tmp_path: Path):
        for level in range(IMPORT_MAX_DEPTH + 2):
            (tmp_path / f"l{level}.md").write_text(f"L{level} @l{level + 1}.md")
        out = resolve_imports("@l0.md", tmp_path / "igni.md", tmp_path)
        expected = " ".join(f"L{n}" for n in range(IMPORT_MAX_DEPTH)) + f" @l{IMPORT_MAX_DEPTH}.md"
        assert out == expected
