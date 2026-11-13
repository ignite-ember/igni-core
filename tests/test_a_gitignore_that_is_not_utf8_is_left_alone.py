"""A ``.igni/.gitignore`` that is not UTF-8 is left alone, not a crash.

Reading it raised ``UnicodeDecodeError``, which is not an ``OSError``, so the
"unreadable means do nothing" branch missed it and the run that keeps the
block current failed the whole session start. A file igni cannot read is not
igni's to rewrite.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.init.gitignore import IgniGitignore


def test_a_gitignore_with_bytes_that_are_not_utf8_is_not_written_and_not_raised(tmp_path: Path):
    raw = b"# caf\xe9\n*.tmp\n"
    (tmp_path / ".gitignore").write_bytes(raw)

    assert IgniGitignore(tmp_path).ensure() is False

    assert (tmp_path / ".gitignore").read_bytes() == raw
