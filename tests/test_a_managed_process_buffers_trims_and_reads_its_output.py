"""``ManagedProcess``'s reader and the reads over its buffer.

The process is a stand-in whose stdout is a real ``asyncio.StreamReader``
the test feeds, so the real reader task runs:

* lines are decoded as utf-8 with replacement and lose their newline;
* past one MiB the oldest half goes, and ``read_tail`` still says how
  much came before what it shows;
* ``read_new`` hands out each line once, in slices of ``max_lines``;
* a backgrounded process tees its lines to the registry and the log and
  ends with a completion; a foreground one emits nothing;
* the exit code is awaited for at most two seconds, and a log that fails
  to write is dropped without stopping the drain;
* ``started_epoch`` is wall-clock.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

import igni_core.core.tools.managed_process as managed_process
from igni_core.core.tools.managed_process import ManagedProcess


class _Registry:
    def __init__(self) -> None:
        self.lines: list[tuple[int, str]] = []
        self.completed: list[Any] = []

    def emit_line(self, pid: int, line: str) -> None:
        self.lines.append((pid, line))

    def emit_completion(self, mp: Any) -> None:
        self.completed.append((mp.finished, mp.returncode()))


class _Log:
    def __init__(self, fail: bool = False) -> None:
        self.written: list[str] = []
        self.closed = False
        self.fail = fail

    def write(self, text: str) -> None:
        if self.fail:
            raise OSError("disk full")
        self.written.append(text)

    def close(self) -> None:
        self.closed = True


class _LogStore:
    def __init__(self, log: _Log | None) -> None:
        self.log = log
        self.opened: list[int] = []

    def open(self, pid: int) -> _Log | None:
        self.opened.append(pid)
        return self.log


class _Proc:
    def __init__(self, pid: int = 77, hang_wait: bool = False) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self._hang = hang_wait

    async def wait(self) -> int:
        if self._hang:
            await asyncio.sleep(60)
        self.returncode = 3
        return 3

    def kill(self) -> None:
        pass

    def feed(self, *chunks: bytes) -> None:
        for chunk in chunks:
            self.stdout.feed_data(chunk)

    def close(self) -> None:
        self.stdout.feed_eof()


def _process(
    proc: _Proc, log: _Log | None = None, *, background: bool = False
) -> tuple[ManagedProcess, _Registry, _LogStore]:
    registry = _Registry()
    store = _LogStore(log)
    supervisor = SimpleNamespace(registry=registry, log_store=store)
    mp = ManagedProcess(proc, "cmd", supervisor)  # type: ignore[arg-type]
    mp.was_backgrounded = background
    return mp, registry, store


async def _drain(mp: ManagedProcess) -> None:
    mp.start_reader()
    assert mp.reader_task is not None
    await asyncio.wait_for(mp.reader_task, timeout=5)


async def test_lines_are_decoded_with_replacement_and_lose_their_newline() -> None:
    proc = _Proc()
    mp, _, _ = _process(proc)
    proc.feed(b"caf\xc3\xa9\n", b"bad \xff byte\n", b"no newline at end")
    proc.close()
    await _drain(mp)
    assert mp.output == ["café", "bad � byte", "no newline at end"]
    assert mp.read() == "café\nbad � byte\nno newline at end"
    assert mp.read(1) == "no newline at end"


async def test_the_exit_code_is_awaited_and_the_process_marked_finished() -> None:
    proc = _Proc()
    mp, _, _ = _process(proc)
    proc.close()
    assert mp.finished is False
    await _drain(mp)
    assert mp.finished is True
    assert mp.is_running() is False
    assert mp.returncode() == 3


async def test_a_wait_that_hangs_does_not_hold_the_reader_past_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_wait_for = asyncio.wait_for
    seen: list[float | None] = []

    async def spy(aw: Any, timeout: float | None = None) -> Any:
        seen.append(timeout)
        return await real_wait_for(aw, timeout=0.05 if timeout == 2.0 else timeout)

    monkeypatch.setattr(managed_process.asyncio, "wait_for", spy)
    proc = _Proc(hang_wait=True)
    mp, _, _ = _process(proc)
    proc.close()
    await _drain(mp)
    assert 2.0 in seen
    assert mp.finished is True
    assert mp.returncode() is None


async def test_past_one_mebibyte_the_oldest_half_goes_and_the_cut_is_counted() -> None:
    proc = _Proc()
    mp, _, _ = _process(proc)
    line = "x" * 1023
    count = 1100  # 1100 * 1023 chars > 1 MiB, so the cap is crossed once
    for i in range(count):
        proc.feed(f"{i:04d}{line[4:]}\n".encode())
    proc.close()
    await _drain(mp)

    assert mp.output[-1].startswith(f"{count - 1:04d}")
    assert len(mp.output) < count
    tail = mp.read_tail(3)
    assert [row[:4] for row in tail.lines] == [f"{count - 3 + i:04d}" for i in range(3)]
    assert tail.total_lines == count
    assert tail.cut is True
    # every line missing from the buffer is accounted for, with its newline
    assert tail.omitted_lines == count - 3
    assert tail.omitted_chars == (count - 3) * 1024
    assert mp._trimmed_lines == count - len(mp.output)


async def test_read_tail_of_an_untrimmed_buffer_counts_only_what_it_hides() -> None:
    proc = _Proc()
    mp, _, _ = _process(proc)
    proc.feed(b"a\nbb\nccc\n")
    proc.close()
    await _drain(mp)
    whole = mp.read_tail(10)
    assert (whole.lines, whole.omitted_lines, whole.cut) == (["a", "bb", "ccc"], 0, False)
    cut = mp.read_tail(1)
    assert (cut.lines, cut.omitted_lines, cut.omitted_chars) == (["ccc"], 2, 5)


async def test_read_new_hands_out_each_line_once_in_slices() -> None:
    proc = _Proc()
    mp, _, _ = _process(proc)
    proc.feed(b"1\n2\n3\n4\n5\n")
    proc.close()
    await _drain(mp)
    assert mp.read_new(2) == "1\n2"
    assert mp.read_new(2) == "3\n4"
    assert mp.read_new(2) == "5"
    assert mp.read_new(2) == ""
    proc2 = _Proc()
    mp2, _, _ = _process(proc2)
    mp2.output = ["only"]
    assert mp2.read_new() == "only"
    mp2.output.append("later")
    assert mp2.read_new() == "later"


async def test_a_backgrounded_process_tees_lines_and_reports_its_end() -> None:
    proc = _Proc(pid=91)
    log = _Log()
    mp, registry, store = _process(proc, log, background=True)
    proc.feed(b"one\ntwo\n")
    proc.close()
    await _drain(mp)
    assert registry.lines == [(91, "one"), (91, "two")]
    assert log.written == ["one\n", "two\n"]
    assert store.opened == [91]
    assert log.closed is True
    # the completion is published after the exit code is known
    assert registry.completed == [(True, 3)]


async def test_a_foreground_process_emits_nothing_and_opens_no_log() -> None:
    proc = _Proc()
    log = _Log()
    mp, registry, store = _process(proc, log, background=False)
    proc.feed(b"quiet\n")
    proc.close()
    await _drain(mp)
    assert mp.output == ["quiet"]
    assert (registry.lines, registry.completed, store.opened, log.written) == ([], [], [], [])


async def test_a_log_that_fails_to_write_is_dropped_and_the_drain_goes_on() -> None:
    proc = _Proc()
    log = _Log(fail=True)
    mp, registry, store = _process(proc, log, background=True)
    proc.feed(b"a\nb\nc\n")
    proc.close()
    await _drain(mp)
    assert mp.output == ["a", "b", "c"]
    assert [line for _, line in registry.lines] == ["a", "b", "c"]
    assert log.closed is True
    # dropped after the first failure, then opened afresh for the next line
    assert store.opened == [77, 77, 77]


async def test_no_log_store_file_means_lines_are_still_emitted() -> None:
    proc = _Proc()
    mp, registry, _ = _process(proc, None, background=True)
    proc.feed(b"a\n")
    proc.close()
    await _drain(mp)
    assert registry.lines == [(77, "a")]


async def test_a_reader_that_errors_is_logged_and_still_finishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    proc = _Proc()
    mp, registry, _ = _process(proc, None, background=True)

    def boom(pid: int, line: str) -> None:
        raise RuntimeError("subscriber blew up")

    registry.emit_line = boom  # type: ignore[method-assign]
    proc.feed(b"a\n")
    proc.close()
    with caplog.at_level("WARNING", logger=managed_process.logger.name):
        await _drain(mp)
    assert mp.finished is True
    assert registry.completed == [(True, 3)]
    assert "reader task for pid=77 errored: subscriber blew up" in caplog.text


async def test_a_cancelled_reader_still_marks_finished_and_propagates() -> None:
    proc = _Proc()
    mp, registry, _ = _process(proc, None, background=True)
    mp.start_reader()
    await asyncio.sleep(0)
    assert mp.reader_task is not None
    mp.reader_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await mp.reader_task
    assert mp.finished is True
    assert registry.completed == [(True, 3)]


async def test_wait_for_reader_without_a_reader_returns_at_once() -> None:
    mp, _, _ = _process(_Proc())
    await asyncio.wait_for(mp.wait_for_reader(5), timeout=1)


async def test_wait_for_reader_gives_up_at_its_timeout_and_cancels_the_reader() -> None:
    proc = _Proc()  # stdout never closes: a child that keeps the pipe open
    mp, _, _ = _process(proc)
    mp.start_reader()
    started = time.monotonic()
    await mp.wait_for_reader(0.1)
    assert time.monotonic() - started < 2
    assert mp.reader_task is not None and mp.reader_task.done()
    assert mp.finished is True


async def test_wait_for_reader_collects_trailing_output() -> None:
    proc = _Proc()
    mp, _, _ = _process(proc)
    mp.start_reader()
    proc.feed(b"tail\n")
    proc.close()
    await mp.wait_for_reader(5)
    assert mp.read() == "tail"


async def test_started_epoch_is_wall_clock_and_elapsed_is_monotonic() -> None:
    mp, _, _ = _process(_Proc())
    mp.started_at = time.monotonic() - 100
    assert abs(mp.started_epoch - (time.time() - 100)) < 2
    assert 99 < mp.elapsed() < 110


async def test_start_reader_registers_a_process_kind_busy_task() -> None:
    from igni_core.core.activity import BusyKind

    proc = _Proc(pid=55)
    mp, _, _ = _process(proc)
    seen: dict[str, Any] = {}
    real = managed_process.background.start

    def spy(coro: Any, **kw: Any) -> Any:
        seen.update(kw)
        return real(coro, **kw)

    managed_process.background.start = spy  # type: ignore[method-assign]
    try:
        mp.start_reader()
    finally:
        managed_process.background.start = real  # type: ignore[method-assign]
    proc.close()
    await mp.wait_for_reader(5)
    assert seen == {"holder": "process:55", "reason": "cmd", "kind": BusyKind.PROCESS}
