"""The run directory watch: what a file's waiter is told, and when the watch lets go.

``docs/CLOUD.md`` §9. The kernel's ``struct inotify_event`` is fed in through
a pipe where inotify itself is not to be had, so the parsing is the real one.
"""

from __future__ import annotations

import asyncio
import os
import struct
from pathlib import Path

import pytest
from igni_cloud.guest.watch import DirectoryWatch, DirectoryWatches, FileWatch


def event(name: bytes, mask: int = DirectoryWatch.IN_MOVED_TO, pad: int = 0) -> bytes:
    """One ``struct inotify_event`` the way the kernel writes it: the name NUL-padded."""
    padded = name + b"\0" * (pad or 1)
    return struct.pack("iIII", 1, mask, 0, len(padded)) + padded


@pytest.fixture
def pipe_watch(tmp_path: Path):
    """A directory watch reading its events from a pipe, in place of an inotify descriptor."""
    reader, writer = os.pipe()
    os.set_blocking(reader, False)
    watch = DirectoryWatch(tmp_path)
    watch._fd = reader
    yield watch, writer
    os.close(writer)
    watch.close()


async def test_a_file_watch_that_nothing_wakes_times_out_false_and_is_clear(
    tmp_path: Path,
) -> None:
    file = DirectoryWatch(tmp_path).file("restored")
    assert await file.wait(0.01) is False
    file.wake(1.0)
    assert await file.wait(1) is True
    assert await file.wait(0.01) is False, "waking is consumed by the wait that saw it"


def test_the_first_unconsumed_event_time_is_kept(tmp_path: Path) -> None:
    file = DirectoryWatch(tmp_path).file("restored")
    assert file.woken_at is None
    file.wake(1.5)
    file.wake(2.5)
    assert file.woken_at == 1.5


def test_a_directory_has_one_watch_however_often_it_is_asked_for(tmp_path: Path) -> None:
    watches = DirectoryWatches()
    assert watches.of(tmp_path) is watches.of(tmp_path)
    assert watches.of(tmp_path) is not watches.of(tmp_path / "other")
    assert watches.of(tmp_path).directory == tmp_path


async def test_two_waiters_of_one_name_are_both_woken(tmp_path: Path, pipe_watch) -> None:
    watch, writer = pipe_watch
    first, second, other = watch.file("restored"), watch.file("restored"), watch.file("x")
    os.write(writer, event(b"restored"))
    watch._readable()
    assert await first.wait(0.5) and await second.wait(0.5)
    assert await other.wait(0.01) is False
    assert first.woken_at is not None


async def test_several_events_in_one_read_wake_each_name_they_name(pipe_watch) -> None:
    watch, writer = pipe_watch
    a, b, c = watch.file("a"), watch.file("b"), watch.file("c")
    os.write(writer, event(b"a", pad=8) + event(b"b", DirectoryWatch.IN_CREATE, pad=16))
    watch._readable()
    assert await a.wait(0.5) and await b.wait(0.5)
    assert await c.wait(0.01) is False, "a file nobody's event named stays asleep"
    assert await watch.wait(0.5), "the directory's own waiter hears of any event"


async def test_a_name_that_is_not_utf8_wakes_nobody_and_does_not_fail(pipe_watch) -> None:
    watch, writer = pipe_watch
    file = watch.file("restored")
    os.write(writer, event(b"\xff\xfe-odd"))
    watch._readable()
    assert await file.wait(0.01) is False


async def test_an_event_nobody_watches_for_still_wakes_the_directory(pipe_watch) -> None:
    watch, writer = pipe_watch
    os.write(writer, event(b"activity.json"))
    watch._readable()
    assert await watch.wait(0.5) is True
    assert await watch.wait(0.01) is False


async def test_a_partial_header_is_ignored(pipe_watch) -> None:
    watch, writer = pipe_watch
    file = watch.file("restored")
    os.write(writer, event(b"restored")[:7])
    watch._readable()
    assert await file.wait(0.01) is False


def test_releasing_a_watch_never_held_is_harmless(tmp_path: Path) -> None:
    directory = DirectoryWatch(tmp_path)
    kept = directory.file("a")
    directory.release(FileWatch(directory, "a"))
    directory.release(FileWatch(directory, "never"))
    assert directory._files == {"a": [kept]}


def test_closing_twice_and_a_reader_that_cannot_be_removed_are_tolerated(
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    directory = DirectoryWatch(tmp_path)
    directory._fd = reader

    class Loop:
        def remove_reader(self, _fd: int) -> None:
            raise RuntimeError("loop closed")

    directory._loop = Loop()  # type: ignore[assignment]
    directory.close()
    directory.close()
    assert directory.watching is False
    with pytest.raises(OSError):
        os.fstat(reader)
    os.close(writer)


async def test_starting_a_watch_already_watching_says_so_without_a_second_one(
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    directory = DirectoryWatch(tmp_path)
    directory._fd = reader
    assert directory.start() is True
    assert directory._fd == reader
    directory.close()
    os.close(writer)


async def test_without_inotify_start_says_no_and_the_file_watch_says_it_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("igni_cloud.guest.watch.sys.platform", "darwin")
    file = DirectoryWatch(tmp_path).file("restored")
    assert file.start() is False
    assert file.watching is False


async def test_the_overflow_event_wakes_every_file(pipe_watch) -> None:
    watch, writer = pipe_watch
    a, b = watch.file("a"), watch.file("b")
    os.write(writer, struct.pack("iIII", -1, DirectoryWatch.IN_Q_OVERFLOW, 0, 0))
    watch._readable()
    assert await asyncio.gather(a.wait(0.5), b.wait(0.5)) == [True, True]
