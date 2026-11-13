"""The shared rules readers: tolerant reads, ordered walks, imports in a root.

Files are real, in a temp directory. A file that cannot be read is
"no rules here", never an error; rules files are UTF-8 whatever the
machine's locale.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from igni_core.core.paths import RULES_FILES, RULES_FILES_CROSS_TOOL
from igni_core.core.utils.context_readers import (
    read_if_exists,
    read_rules_dir,
    read_rules_dir_files,
    read_with_imports,
    rules_filenames,
)


def test_a_missing_file_a_directory_and_an_empty_file_read_as_nothing(tmp_path):
    (tmp_path / "empty.md").write_text("")
    (tmp_path / "dir.md").mkdir()
    assert read_if_exists(tmp_path / "absent.md") == ""
    assert read_if_exists(tmp_path / "dir.md") == ""
    assert read_if_exists(tmp_path / "empty.md") == ""
    assert read_with_imports(tmp_path / "empty.md", tmp_path) == ""


def test_a_file_that_is_not_valid_utf8_reads_as_nothing_and_says_why_at_debug(tmp_path, caplog):
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00 not text")
    with caplog.at_level("DEBUG", logger="igni_core.core.utils.context_readers"):
        assert read_if_exists(bad) == ""
    assert "Failed to read rules from" in caplog.text
    assert str(bad) in caplog.text


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a permission-denied file")
def test_an_unreadable_file_reads_as_nothing_instead_of_raising(tmp_path):
    locked = tmp_path / "locked.md"
    locked.write_text("secret")
    locked.chmod(0)
    try:
        assert read_if_exists(locked) == ""
    finally:
        locked.chmod(0o600)


def test_rules_are_read_as_utf8_on_a_machine_whose_locale_is_not(tmp_path):
    (tmp_path / "igni.md").write_text("naïve — “quoted” ✓", encoding="utf-8")
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from igni_core.core.utils.context_readers import read_if_exists\n"
        "text = read_if_exists(Path(sys.argv[1]) / 'igni.md')\n"
        "sys.stdout.buffer.write(text.encode('utf-8'))\n"
    )
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    done = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env=env,
        capture_output=True,
        check=True,
    )
    assert done.stdout.decode("utf-8") == "naïve — “quoted” ✓"


def test_the_filenames_follow_the_cross_tool_flag_with_local_overrides_after_the_base():
    assert rules_filenames(True) == RULES_FILES_CROSS_TOOL
    assert rules_filenames(False) == RULES_FILES
    assert rules_filenames() == RULES_FILES_CROSS_TOOL
    assert "CLAUDE.md" in rules_filenames(True)
    assert "CLAUDE.md" not in rules_filenames(False)
    for names in (rules_filenames(True), rules_filenames(False)):
        assert names.index("igni.md") < names.index("igni.local.md")


def test_a_rules_dir_joins_the_files_it_finds_in_the_order_asked(tmp_path):
    (tmp_path / "b.md").write_text("B")
    (tmp_path / "a.md").write_text("A")
    assert read_rules_dir(tmp_path, ("b.md", "missing.md", "a.md")) == "B\n\nA"
    assert read_rules_dir(tmp_path, ("missing.md",)) == ""


def test_a_rules_dir_resolves_imports_in_the_root_it_is_given_not_its_own(tmp_path):
    project = tmp_path / "project"
    sub = project / "sub"
    sub.mkdir(parents=True)
    (project / "shared.md").write_text("SHARED")
    (sub / "igni.md").write_text("@../shared.md")
    assert read_rules_dir(sub, ("igni.md",), allowed_root=project) == "SHARED"
    # With no root the directory itself bounds the import, so it stays literal.
    assert read_rules_dir(sub, ("igni.md",)) == "@../shared.md"


def _rules(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def test_a_rules_tree_is_walked_recursively_in_sorted_order(tmp_path):
    _rules(tmp_path, {"b.md": "B", "a.md": "A", "nested/z.md": "Z", "nested/deeper/c.md": "C"})
    assert read_rules_dir_files(tmp_path) == "A\n\nB\n\nC\n\nZ"


def test_files_that_are_not_markdown_or_are_directories_named_md_are_ignored(tmp_path):
    _rules(tmp_path, {"a.md": "A", "notes.txt": "NOT MARKDOWN"})
    (tmp_path / "folder.md").mkdir()
    (tmp_path / "folder.md" / "inside.md").write_text("INSIDE")
    assert read_rules_dir_files(tmp_path) == "A\n\nINSIDE"


def test_a_file_with_only_frontmatter_or_only_whitespace_contributes_nothing(tmp_path):
    (tmp_path / "front.md").write_text("---\ndescription: nothing else\n---\n")
    (tmp_path / "blank.md").write_text("  \n\n  ")
    (tmp_path / "real.md").write_text("---\ndescription: x\n---\n\nREAL\n")
    assert read_rules_dir_files(tmp_path) == "REAL"


def test_a_missing_rules_dir_and_a_path_that_is_a_file_give_nothing(tmp_path):
    (tmp_path / "file.md").write_text("X")
    assert read_rules_dir_files(tmp_path / "absent") == ""
    assert read_rules_dir_files(tmp_path / "file.md") == ""


def test_a_path_scoped_file_needs_a_working_dir_that_matches(tmp_path):
    rules = tmp_path / "rules"
    _rules(rules, {"scoped.md": "---\npaths:\n  - src*\n---\nSCOPED", "open.md": "OPEN"})
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    assert read_rules_dir_files(rules) == "OPEN"
    assert read_rules_dir_files(rules, tmp_path / "docs", tmp_path) == "OPEN"
    assert read_rules_dir_files(rules, tmp_path / "src", tmp_path) == "OPEN\n\nSCOPED"


def test_imports_inside_a_rules_tree_stay_inside_that_tree(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (tmp_path / "outside.md").write_text("OUTSIDE")
    (rules / "inside.md").write_text("INSIDE")
    (rules / "a.md").write_text("@inside.md and @../outside.md")
    out = read_rules_dir_files(rules)
    assert "INSIDE" in out
    assert "OUTSIDE" not in out
