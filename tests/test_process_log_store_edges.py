"""The per-pid log store's reads and writes at their edges.

``ProcessLogStore`` is what lets a backgrounded process's output outlive
the BE. The happy path is pinned in ``test_process_log.py``; this file
pins what the reader sees when the file is odd: empty, not UTF-8, a
directory, mid-write, or shared with a previous process that had the
same pid.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.paths import CONFIG_DIR
from igni_core.core.tools.process_log import ProcessLogStore


@pytest.fixture
def store(tmp_path: Path) -> ProcessLogStore:
    return ProcessLogStore(tmp_path)


def _write_bytes(store: ProcessLogStore, pid: int, data: bytes) -> Path:
    path = store.path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_unreadable_as_file(store: ProcessLogStore, pid: int) -> None:
    """A directory where the log file should be: ``open`` raises OSError."""
    store.path(pid).mkdir(parents=True)


class TestTheTmpFallbackCarriesTheProductName:
    def test_the_fallback_directory_is_igni_process_logs(self) -> None:
        # The rename left one old name behind in the fallback directory.
        path = ProcessLogStore(None).path(7)
        assert path.parent.name == "igni-process-logs"
        assert "ember" not in str(path)

    def test_a_project_store_does_not_use_the_fallback(self, tmp_path: Path) -> None:
        assert ProcessLogStore(tmp_path).path(7).parent == tmp_path / CONFIG_DIR / "process_logs"


class TestLines:
    """C3: ``lines`` lets a tail say how much came before it."""

    def test_every_line_without_newlines(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, b"a\nb\nc\n")
        assert store.lines(1) == ["a", "b", "c"]

    def test_a_missing_file_has_no_lines(self, store: ProcessLogStore) -> None:
        assert store.lines(1) == []

    def test_an_empty_file_has_no_lines_not_one_empty_line(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, b"")
        assert store.lines(1) == []

    def test_a_blank_line_in_the_middle_is_kept(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, b"a\n\nb\n")
        assert store.lines(1) == ["a", "", "b"]

    def test_a_file_without_a_trailing_newline_keeps_its_last_line(
        self, store: ProcessLogStore
    ) -> None:
        _write_bytes(store, 1, b"a\nb")
        assert store.lines(1) == ["a", "b"]

    def test_a_path_that_cannot_be_read_has_no_lines(self, store: ProcessLogStore) -> None:
        _make_unreadable_as_file(store, 1)
        assert store.lines(1) == []

    def test_invalid_utf8_is_replaced_not_raised(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, b"ok\n\xff\xfe bad\n")
        assert store.lines(1) == ["ok", "�� bad"]

    def test_unicode_survives(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, "héllo\n日本語\n".encode())
        assert store.lines(1) == ["héllo", "日本語"]


class TestTail:
    def test_an_empty_file_tails_to_the_empty_string(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, b"")
        assert store.tail(1) == ""

    def test_a_path_that_cannot_be_read_tails_to_the_empty_string(
        self, store: ProcessLogStore
    ) -> None:
        _make_unreadable_as_file(store, 1)
        assert store.tail(1) == ""

    def test_invalid_utf8_is_replaced_not_raised(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, b"\xff\n")
        assert store.tail(1) == "�"

    def test_a_negative_count_is_empty(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, b"a\nb\n")
        assert store.tail(1, -3) == ""

    def test_a_count_beyond_the_file_returns_all_of_it(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, b"a\nb\n")
        assert store.tail(1, 1000) == "a\nb"

    def test_the_default_is_the_last_two_hundred_lines(self, store: ProcessLogStore) -> None:
        _write_bytes(store, 1, "".join(f"{i}\n" for i in range(250)).encode())
        got = store.tail(1).split("\n")
        assert got[0] == "50" and got[-1] == "249" and len(got) == 200


class TestOpen:
    def test_a_write_is_visible_to_a_reader_before_the_file_is_closed(
        self, store: ProcessLogStore
    ) -> None:
        # Line buffering: an orphan reading mid-stream sees the line.
        f = store.open(1)
        assert f is not None
        try:
            f.write("live\n")
            assert store.tail(1) == "live"
        finally:
            f.close()

    def test_a_reopened_log_keeps_what_a_previous_process_with_the_pid_wrote(
        self, store: ProcessLogStore
    ) -> None:
        for text in ("old\n", "new\n"):
            f = store.open(1)
            assert f is not None
            f.write(text)
            f.close()
        assert store.lines(1) == ["old", "new"]

    def test_non_utf8_text_is_written_not_raised(self, store: ProcessLogStore) -> None:
        f = store.open(1)
        assert f is not None
        f.write("lone surrogate \ud800\n")
        f.close()
        assert store.lines(1) == ["lone surrogate ?"]

    def test_a_non_integer_pid_is_refused(self, store: ProcessLogStore) -> None:
        with pytest.raises(ValueError):
            store.path("abc")  # type: ignore[arg-type]

    def test_a_numeric_string_pid_is_coerced(self, store: ProcessLogStore) -> None:
        assert store.path("12").name == "12.log"  # type: ignore[arg-type]


class TestCleanup:
    def test_it_deletes_the_file_and_is_idempotent(self, store: ProcessLogStore) -> None:
        path = _write_bytes(store, 1, b"x\n")
        store.cleanup(1)
        store.cleanup(1)
        assert not path.exists()

    def test_an_error_other_than_missing_is_swallowed(self, store: ProcessLogStore) -> None:
        # ``os.remove`` on a directory raises IsADirectoryError / PermissionError.
        _make_unreadable_as_file(store, 1)
        store.cleanup(1)
        assert store.path(1).is_dir()


class TestSetProjectDir:
    def test_the_root_follows_the_setter(self, tmp_path: Path) -> None:
        store = ProcessLogStore(None)
        store.set_project_dir(tmp_path)
        assert store.project_dir == tmp_path
        assert store.path(3) == tmp_path / CONFIG_DIR / "process_logs" / "3.log"
        store.set_project_dir(None)
        assert store.project_dir is None
