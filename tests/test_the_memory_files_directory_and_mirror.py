"""The desktop's memory directory and the mirror that writes it through to a store.

Real directories in a temp directory. The mirror's store is a second real
directory, so what it keeps is read back the way the agent's tools read it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from igni_core.core.utils.memory_files import DirectoryMemoryFiles, MemoryMirror


def test_a_missing_directory_has_no_files_and_a_write_creates_it(tmp_path: Path) -> None:
    files = DirectoryMemoryFiles(tmp_path / "deep" / "memory")

    assert files.names() == []
    assert files.read("MEMORY.md") is None
    assert files.delete("MEMORY.md") is False

    files.write("MEMORY.md", "- [a](a.md)\n")

    assert (tmp_path / "deep" / "memory" / "MEMORY.md").read_text() == "- [a](a.md)\n"


def test_empty_content_reads_back_as_empty_not_as_missing(tmp_path: Path) -> None:
    files = DirectoryMemoryFiles(tmp_path)
    files.write("e.md", "")

    assert files.read("e.md") == ""
    assert files.names() == ["e.md"]


def test_a_file_that_is_not_utf8_or_is_a_directory_reads_as_missing(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe\x00 not text")
    (tmp_path / "sub").mkdir()
    files = DirectoryMemoryFiles(tmp_path)

    assert files.read("bad.md") is None
    assert files.read("sub") is None


def test_the_names_are_files_only_and_sorted_with_capitals_first(tmp_path: Path) -> None:
    for name in ("b.md", "MEMORY.md", "a.md"):
        (tmp_path / name).write_text("x")
    (tmp_path / "sub").mkdir()
    files = DirectoryMemoryFiles(tmp_path)

    assert files.names() == ["MEMORY.md", "a.md", "b.md"]


def test_deleting_removes_a_file_and_leaves_a_directory_alone(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "sub").mkdir()
    files = DirectoryMemoryFiles(tmp_path)

    assert files.delete("a.md") is True
    assert not (tmp_path / "a.md").exists()
    assert files.delete("sub") is False
    assert (tmp_path / "sub").is_dir()


def _mirror(tmp_path: Path) -> tuple[MemoryMirror, DirectoryMemoryFiles, Path]:
    store = DirectoryMemoryFiles(tmp_path / "store")
    cache = tmp_path / "cache"
    return MemoryMirror(store, cache), store, cache


def test_fill_makes_the_directory_the_stored_files_and_removes_strays(tmp_path: Path) -> None:
    mirror, store, cache = _mirror(tmp_path)
    store.write("MEMORY.md", "index")
    store.write("empty.md", "")
    cache.mkdir()
    (cache / "stray.md").write_text("left over")
    (cache / "sub").mkdir()
    (cache / "MEMORY.md").write_text("stale")

    assert mirror.fill() == 2

    assert sorted(p.name for p in cache.iterdir()) == ["MEMORY.md", "empty.md", "sub"]
    assert (cache / "MEMORY.md").read_text() == "index"
    assert (cache / "empty.md").read_text() == ""
    assert mirror.directory == cache


def test_sync_stores_what_was_written_changed_and_removed_and_counts_it(tmp_path: Path) -> None:
    mirror, store, cache = _mirror(tmp_path)
    store.write("old.md", "old")
    store.write("gone.md", "gone")
    mirror.fill()
    assert mirror.sync() == 0

    (cache / "new.md").write_text("new")
    (cache / "old.md").write_text("changed")
    (cache / "gone.md").unlink()

    assert mirror.sync() == 3
    assert store.read("new.md") == "new"
    assert store.read("old.md") == "changed"
    assert store.read("gone.md") is None
    assert mirror.sync() == 0


def test_a_file_written_empty_is_stored_empty(tmp_path: Path) -> None:
    mirror, store, cache = _mirror(tmp_path)
    mirror.fill()
    (cache / "e.md").write_text("")

    assert mirror.sync() == 1
    assert store.read("e.md") == ""


def test_sync_with_no_directory_and_nothing_known_changes_nothing(tmp_path: Path) -> None:
    mirror, store, _ = _mirror(tmp_path)

    assert mirror.sync() == 0
    assert store.names() == []


def test_a_file_that_cannot_be_read_is_skipped_and_its_stored_copy_kept(tmp_path: Path) -> None:
    mirror, store, cache = _mirror(tmp_path)
    store.write("keep.md", "kept")
    mirror.fill()
    (cache / "keep.md").write_bytes(b"\xff\xfe\x00 not text")

    assert mirror.sync() == 0
    assert store.read("keep.md") == "kept"


class _FailingStore(DirectoryMemoryFiles):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.fail = True

    def write(self, name: str, content: str) -> None:
        if self.fail:
            raise OSError("database is down")
        super().write(name, content)


def test_a_failed_write_is_logged_and_retried_at_the_next_sync(tmp_path: Path, caplog) -> None:
    store = _FailingStore(tmp_path / "store")
    cache = tmp_path / "cache"
    mirror = MemoryMirror(store, cache)
    mirror.fill()
    (cache / "a.md").write_text("a")

    with caplog.at_level("WARNING", logger="igni_core.core.utils.memory_files"):
        assert mirror.sync() == 0
    assert "memory files: sync failed: database is down" in caplog.text
    assert store.read("a.md") is None

    store.fail = False
    assert mirror.sync() == 1
    assert store.read("a.md") == "a"


@pytest.mark.skipif(os.name == "nt", reason="locale is a POSIX notion")
def test_memory_is_utf8_on_a_machine_whose_locale_is_not(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from igni_core.core.utils.memory_files import DirectoryMemoryFiles, MemoryMirror\n"
        "root = Path(sys.argv[1])\n"
        "store = DirectoryMemoryFiles(root / 'store')\n"
        "store.write('MEMORY.md', 'naïve — “quoted” ✓')\n"
        "mirror = MemoryMirror(store, root / 'cache')\n"
        "assert mirror.fill() == 1\n"
        "(root / 'cache' / 'new.md').write_text('ünï', encoding='utf-8')\n"
        "assert mirror.sync() == 1\n"
        "out = [store.read('MEMORY.md'), store.read('new.md'),\n"
        "       (root / 'cache' / 'MEMORY.md').read_text(encoding='utf-8')]\n"
        "sys.stdout.buffer.write('|'.join(out).encode('utf-8'))\n"
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
        [sys.executable, "-c", code, str(tmp_path)], env=env, capture_output=True, check=True
    )
    assert done.stdout.decode("utf-8") == "naïve — “quoted” ✓|ünï|naïve — “quoted” ✓"
