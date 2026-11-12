"""The suspend watch's answer, and what the heap trim does with what the system tells it.

``docs/CLOUD_RESUME_TODO.md`` item 2.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from igni_cloud.guest.suspend import HeapTrim, MallInfo2, SuspendWatch

PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024


def watch_in(tmp_path: Path, **kwargs: Any) -> SuspendWatch:
    return SuspendWatch(tmp_path / "suspending", tmp_path / "suspend-ready", watch=False, **kwargs)


def ask(tmp_path: Path, token: str) -> None:
    (tmp_path / "suspending").write_text(json.dumps({"token": token}))


class Libc:
    """glibc as far as the trim sees it; ``statm`` shrinks by ``gives_back_pages`` per trim."""

    def __init__(self, statm: Path, free_kb: int, gives_back_pages: int = 0) -> None:
        self.statm, self.gives_back = statm, gives_back_pages
        self.trims = 0
        self.resident = 1000
        statm.write_text(f"5000 {self.resident} 10\n")
        free = free_kb * 1024

        class Info:
            restype: Any = None

            def __call__(self) -> MallInfo2:
                return MallInfo2(fordblks=free)

        self.mallinfo2 = Info()

    def malloc_trim(self, _pad: int) -> int:
        self.trims += 1
        if self.gives_back is None:
            return 1
        self.resident -= self.gives_back
        self.statm.write_text(f"5000 {self.resident} 10\n")
        return 1


@pytest.fixture
def libc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Libc:
    fake = Libc(tmp_path / "statm", free_kb=HeapTrim.MIN_FREE_KB, gives_back_pages=100)
    monkeypatch.setattr(HeapTrim, "_libc", classmethod(lambda cls: fake))
    monkeypatch.setattr(HeapTrim, "_STATM", fake.statm)
    return fake


@pytest.mark.parametrize(
    "content",
    [None, "", "{", '{"token": 5}', '{"other": "x"}', "[]", '"t"'],
)
def test_a_request_that_is_missing_or_malformed_is_no_request(
    tmp_path: Path, content: str | None
) -> None:
    watch = watch_in(tmp_path)
    if content is not None:
        (tmp_path / "suspending").write_text(content)
    assert watch.read() is None


async def test_a_request_that_is_not_one_is_not_answered(tmp_path: Path) -> None:
    watch = watch_in(tmp_path)
    (tmp_path / "suspending").write_text("{")
    assert await watch.check() is None
    assert not (tmp_path / "suspend-ready").exists()


async def test_a_new_token_is_answered_after_the_old_one_and_the_file_says_all(
    tmp_path: Path, libc: Libc
) -> None:
    watch = watch_in(tmp_path)
    ask(tmp_path, "one")
    first = await watch.check()
    ask(tmp_path, "two")
    second = await watch.check()
    assert first is not None and second is not None
    written = json.loads((tmp_path / "suspend-ready").read_text())
    assert written["token"] == "two"
    assert set(written) == {"token", "ms", "unfinished", "trimmed_kb", "free_heap_kb"}
    assert written["unfinished"] == []
    assert (written["trimmed_kb"], written["free_heap_kb"]) == (100 * PAGE_KB, HeapTrim.MIN_FREE_KB)


async def test_a_step_that_raises_is_left_unfinished_and_the_rest_still_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    watch = watch_in(tmp_path)
    ran: list[str] = []

    async def breaks() -> None:
        raise RuntimeError("pool gone")

    async def after() -> None:
        ran.append("after")

    watch.before_suspend(breaks)
    watch.before_suspend(after)
    ask(tmp_path, "t")
    answer = await watch.check()
    assert answer is not None and answer.unfinished == ["breaks"]
    assert ran == ["after"]
    assert "before-suspend step breaks failed" in caplog.text
    assert "pool gone" in caplog.text


async def test_steps_share_one_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SuspendWatch, "STEPS_SECONDS", 0.3)
    watch = watch_in(tmp_path)

    async def first() -> None:
        await asyncio.sleep(5)

    async def second() -> None:
        await asyncio.sleep(5)

    watch.before_suspend(first)
    watch.before_suspend(second)
    ask(tmp_path, "t")
    answer = await asyncio.wait_for(watch.check(), 2)
    assert answer is not None and answer.unfinished == ["first", "second"]
    assert answer.ms < 500, "the second step had only what the first left"


async def test_a_ready_file_that_cannot_be_written_is_retried(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    watch = watch_in(tmp_path)
    (tmp_path / "suspend-ready").mkdir()  # a directory where the answer goes
    ask(tmp_path, "t")
    assert await watch.check() is None
    assert "cannot answer" in caplog.text
    (tmp_path / "suspend-ready").rmdir()
    answer = await watch.check()
    assert answer is not None and answer.token == "t", "the token was not marked answered"
    assert json.loads((tmp_path / "suspend-ready").read_text())["token"] == "t"


async def test_a_request_left_from_before_is_not_answered_but_the_next_is(
    tmp_path: Path,
) -> None:
    watch = watch_in(tmp_path)
    ask(tmp_path, "stale")
    watch.start()
    first_task = watch._task
    watch.start()
    assert watch._task is first_task, "one task only"
    try:
        assert await watch.check() is None
        assert not (tmp_path / "suspend-ready").exists()
        ask(tmp_path, "fresh")
        answer = await watch.check()
        assert answer is not None and answer.token == "fresh"
    finally:
        await watch.stop()


async def test_started_with_no_request_the_first_one_is_answered(tmp_path: Path) -> None:
    watch = watch_in(tmp_path)
    watch.start()
    try:
        ask(tmp_path, "first")
        answer = await watch.check()
        assert answer is not None and answer.token == "first"
    finally:
        await watch.stop()


async def test_stopping_closes_the_file_watch_and_can_be_done_twice(tmp_path: Path) -> None:
    await SuspendWatch(tmp_path / "suspending", tmp_path / "suspend-ready").stop()  # not started
    watch = SuspendWatch(tmp_path / "suspending", tmp_path / "suspend-ready")
    watch.start()
    assert watch._directory is not None
    directory = watch._directory.directory
    assert "suspending" in directory._files
    await watch.stop()
    await watch.stop()
    assert watch._task is None
    assert directory._files == {}, "the file watch let go of the directory"


async def test_without_inotify_it_looks_every_poll_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SuspendWatch, "POLL_SECONDS", 0.01)
    monkeypatch.setattr("igni_cloud.guest.watch.sys.platform", "darwin")
    watch = SuspendWatch(tmp_path / "suspending", tmp_path / "suspend-ready")
    watch.start()
    try:
        ask(tmp_path, "polled")
        for _ in range(200):
            if (tmp_path / "suspend-ready").exists():
                break
            await asyncio.sleep(0.01)
        assert json.loads((tmp_path / "suspend-ready").read_text())["token"] == "polled"
    finally:
        await watch.stop()


def test_free_exactly_at_the_threshold_is_given_back(libc: Libc) -> None:
    assert HeapTrim.run(HeapTrim.MIN_FREE_KB - 1) == 0
    assert libc.trims == 0
    assert HeapTrim.run(HeapTrim.MIN_FREE_KB) == 100 * PAGE_KB
    assert libc.trims == 1


def test_when_it_cannot_tell_what_is_free_it_trims(libc: Libc) -> None:
    assert HeapTrim.run(None) == 100 * PAGE_KB
    assert libc.trims == 1


def test_a_resident_set_that_grew_is_a_shrink_of_nothing(libc: Libc) -> None:
    libc.gives_back = -50
    assert HeapTrim.run(None) == 0


@pytest.mark.parametrize("statm", [None, "", "garbled", "1 x 3"])
def test_a_resident_set_that_cannot_be_read_gives_no_figure(libc: Libc, statm: str | None) -> None:
    if statm is None:
        libc.statm.unlink()
    else:
        libc.statm.write_text(statm)
    libc.gives_back = None  # type: ignore[assignment]  # a trim that leaves statm as it was
    assert HeapTrim.run(None) is None
    assert libc.trims == 1, "it trimmed all the same"
    assert HeapTrim._resident_kb() is None


def test_where_there_is_no_glibc_it_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HeapTrim, "_libc", classmethod(lambda cls: None))
    assert HeapTrim.free_kb() is None
    assert HeapTrim.run() is None
    assert HeapTrim.run(10**9) is None


def test_a_libc_without_the_glibc_calls_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HeapTrim, "_libc", classmethod(lambda cls: object()))
    assert HeapTrim.free_kb() is None
    assert HeapTrim.run(None) is None


def test_not_on_linux_there_is_no_libc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("igni_cloud.guest.suspend.sys.platform", "darwin")
    assert HeapTrim._libc() is None
