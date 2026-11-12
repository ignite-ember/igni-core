"""The restore watch and the next-answer wait, at their edges.

``docs/CLOUD.md`` §9, ``igni_cloud/guest/resume.py``.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import struct
import time
from pathlib import Path
from typing import Any

import pytest
from igni_cloud.guest.resume import NextAnswer, Restored, RestoreStep, RestoreWatch
from igni_cloud.guest.watch import DirectoryWatch


@pytest.fixture(autouse=True)
def _collector_as_found() -> Any:
    was = gc.isenabled()
    yield
    gc.enable() if was else gc.disable()


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 0.3)
    monkeypatch.setattr(RestoreWatch, "AFTER_ANSWER_SECONDS", 0.01)
    monkeypatch.setattr(RestoreWatch, "POLL_SECONDS", 0.01)


def write(path: Path, generation: int, **rest: Any) -> None:
    path.write_text(json.dumps({"generation": generation, **rest}))


@pytest.mark.parametrize(
    "content",
    [None, "", "{", "[]", '{"generation": "x"}', '{"at_ms": 5}', '{"generation": 1.5}', "null"],
)
def test_a_restored_file_that_is_missing_or_not_one_reads_as_none(
    tmp_path: Path, content: str | None
) -> None:
    watch = RestoreWatch(tmp_path / "restored", watch=False)
    if content is not None:
        (tmp_path / "restored").write_text(content)
    assert watch.read() is None


def test_only_the_generation_is_needed_and_the_rest_defaults_to_nothing(tmp_path: Path) -> None:
    write(tmp_path / "restored", 4)
    assert RestoreWatch(tmp_path / "restored", watch=False).read() == Restored(generation=4)
    write(tmp_path / "restored", 5, at_ms=7, clock_step_ms=2.5)
    assert RestoreWatch(tmp_path / "restored", watch=False).read() == Restored(
        generation=5, at_ms=7, clock_step_ms=2.5
    )


async def test_the_restore_found_when_it_began_watching_runs_nothing(tmp_path: Path) -> None:
    path = tmp_path / "restored"
    watch = RestoreWatch(path, watch=False)
    ran: list[int] = []

    async def step(restored: Restored) -> None:
        ran.append(restored.generation)

    watch.on_restore(step)
    write(path, 3)
    assert await watch.check() is None, "the first look is the baseline"
    assert await watch.check() is None
    assert ran == [] and watch.timeline is None
    write(path, 4)
    restored = await watch.check()
    assert restored is not None and restored.generation == 4
    assert ran == [4]


async def test_a_restored_file_that_appears_after_start_is_a_restore(tmp_path: Path) -> None:
    path = tmp_path / "restored"
    watch = RestoreWatch(path, watch=False)
    ran: list[int] = []

    async def step(restored: Restored) -> None:
        ran.append(restored.generation)

    watch.on_restore(step)
    watch.start()
    try:
        write(path, 1)
        assert await watch.check() is not None
        assert ran == [1]
    finally:
        await watch.stop()


async def test_a_generation_that_goes_down_is_a_new_restore_and_the_same_is_not(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restored"
    watch = RestoreWatch(path, watch=False)
    ran: list[int] = []

    async def step(restored: Restored) -> None:
        ran.append(restored.generation)

    watch.on_restore(step)
    write(path, 5)
    await watch.check()
    write(path, 2)
    await watch.check()
    await watch.check()
    write(path, 9)
    await watch.check()
    assert ran == [2, 9]


async def test_a_step_with_no_phase_given_runs_at_once(tmp_path: Path) -> None:
    path = tmp_path / "restored"
    watch = RestoreWatch(path, watch=False)

    async def step(_restored: Restored) -> None: ...

    watch.on_restore(step)
    assert watch._steps_by[RestoreStep.AT_ONCE] == [step]
    assert watch._steps_by[RestoreStep.LATER] == []


async def test_a_restore_noticed_while_the_last_is_unfinished_finishes_its_timeline(
    tmp_path: Path, quick: None
) -> None:
    path = tmp_path / "restored"
    watch = RestoreWatch(path, watch=False)
    write(path, 1)
    await watch.check()
    write(path, 2)
    await watch.check()
    first = watch.timeline
    assert first is not None
    write(path, 3)
    await watch.check()
    assert first.done
    assert watch.timeline is not first and watch.timeline is not None
    assert watch.timeline.record.generation == 3


async def test_answering_with_no_restore_is_harmless(tmp_path: Path) -> None:
    watch = RestoreWatch(tmp_path / "restored", watch=False)
    watch.answered()
    assert watch.waiting_for_answer is False


async def test_it_is_waiting_for_the_answer_only_while_it_waits(
    tmp_path: Path, quick: None
) -> None:
    path = tmp_path / "restored"
    watch = RestoreWatch(path, watch=False)
    seen: list[bool] = []

    async def at_answer(_restored: Restored) -> None:
        seen.append(watch.waiting_for_answer)

    watch.on_restore(at_answer, when=RestoreStep.AT_FIRST_ANSWER)
    write(path, 1)
    await watch.check()
    assert watch.waiting_for_answer is False
    write(path, 2)
    checking = asyncio.create_task(watch.check())
    for _ in range(100):
        if watch.waiting_for_answer:
            break
        await asyncio.sleep(0.001)
    assert watch.waiting_for_answer is True
    watch.answered()
    await checking
    assert seen == [True]
    assert watch.waiting_for_answer is False


async def test_the_collector_is_back_when_the_wait_is_cancelled(
    tmp_path: Path, quick: None
) -> None:
    path = tmp_path / "restored"
    watch = RestoreWatch(path, watch=False)

    async def later(_restored: Restored) -> None: ...

    watch.on_restore(later, when=RestoreStep.LATER)
    write(path, 1)
    await watch.check()
    write(path, 2)
    gc.enable()
    checking = asyncio.create_task(watch.check())
    for _ in range(100):
        if watch.waiting_for_answer:
            break
        await asyncio.sleep(0.001)
    assert not gc.isenabled(), "off while the steps wait"
    checking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await checking
    assert gc.isenabled()
    assert watch.waiting_for_answer is False


async def test_a_collector_that_was_off_is_left_off(tmp_path: Path, quick: None) -> None:
    path = tmp_path / "restored"
    watch = RestoreWatch(path, watch=False)
    write(path, 1)
    await watch.check()
    gc.disable()
    write(path, 2)
    await watch.check()
    assert not gc.isenabled(), "the caller's choice, not ours to undo"


async def test_a_collector_is_back_when_an_after_restore_step_ends_the_check_badly(
    tmp_path: Path, quick: None
) -> None:
    path = tmp_path / "restored"
    watch = RestoreWatch(path, watch=False)

    async def stop_it(_restored: Restored) -> None:
        raise asyncio.CancelledError

    watch.on_restore(stop_it)
    write(path, 1)
    await watch.check()
    gc.enable()
    write(path, 2)
    with pytest.raises(asyncio.CancelledError):
        await watch.check()
    assert gc.isenabled()


async def test_stopping_before_starting_and_twice_closes_the_file_watch(tmp_path: Path) -> None:
    await RestoreWatch(tmp_path / "restored").stop()
    watch = RestoreWatch(tmp_path / "restored")
    watch.start()
    first = watch._task
    watch.start()
    assert watch._task is first, "one task only"
    directory = watch._directory.directory  # type: ignore[union-attr]
    assert "restored" in directory._files
    await watch.stop()
    await watch.stop()
    assert watch._task is None and first is not None and first.done()
    assert directory._files == {}


async def test_without_inotify_the_directory_is_looked_at_every_poll(
    tmp_path: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("igni_cloud.guest.watch.sys.platform", "darwin")
    path = tmp_path / "restored"
    write(path, 1)
    watch = RestoreWatch(path)
    ran: list[int] = []

    async def step(restored: Restored) -> None:
        ran.append(restored.generation)

    watch.on_restore(step)
    watch.start()
    try:
        write(path, 2)
        for _ in range(300):
            if ran:
                break
            await asyncio.sleep(0.01)
        assert ran == [2]
    finally:
        await watch.stop()


async def test_the_next_answer_ends_the_wait_and_the_burst_follows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 5.0)
    monkeypatch.setattr(RestoreWatch, "AFTER_ANSWER_SECONDS", 0.2)
    waiting = NextAnswer()
    waited = asyncio.create_task(waiting.after())
    await asyncio.sleep(0.01)
    started = time.perf_counter()
    waiting.answered()
    await waited
    assert time.perf_counter() - started >= 0.18, "the burst's moment after the answer"


async def test_without_a_burst_the_answer_ends_it_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 5.0)
    monkeypatch.setattr(RestoreWatch, "AFTER_ANSWER_SECONDS", 2.0)
    waiting = NextAnswer()
    waited = asyncio.create_task(waiting.after(burst=False))
    await asyncio.sleep(0.01)
    waiting.answered()
    await asyncio.wait_for(waited, 1)


async def test_no_answer_ends_the_wait_at_the_limit_with_no_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 0.05)
    monkeypatch.setattr(RestoreWatch, "AFTER_ANSWER_SECONDS", 2.0)
    started = time.perf_counter()
    await asyncio.wait_for(NextAnswer().after(), 1)
    assert time.perf_counter() - started < 1.0


async def test_an_answer_from_before_the_wait_does_not_end_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 0.2)
    monkeypatch.setattr(RestoreWatch, "AFTER_ANSWER_SECONDS", 3.0)
    waiting = NextAnswer()
    waiting.answered()
    started = time.perf_counter()
    await asyncio.wait_for(waiting.after(), 2)
    assert time.perf_counter() - started >= 0.18, "waited the full limit: it is the next answer"


async def test_the_time_the_event_was_read_reaches_the_timeline(
    tmp_path: Path, quick: None
) -> None:
    """The loop's wake-up is what ``woke_after_write_ms`` measures: it must travel with the event."""
    path = tmp_path / "restored"
    write(path, 1)
    directory = DirectoryWatch(tmp_path)
    reader, writer = os.pipe()
    os.set_blocking(reader, False)
    loop = asyncio.get_running_loop()
    directory._fd, directory._loop = reader, loop
    loop.add_reader(reader, directory._readable)
    watch = RestoreWatch(path, directory=directory)
    watch.start()
    try:
        write(path, 2, at_ms=int(time.time() * 1000) - 10)
        name = b"restored\0"
        os.write(writer, struct.pack("iIII", 1, DirectoryWatch.IN_MOVED_TO, 0, len(name)) + name)
        for _ in range(300):
            if watch.timeline is not None:
                break
            await asyncio.sleep(0.01)
        assert watch.timeline is not None
        assert watch.timeline.record.woke_after_write_ms is not None
    finally:
        await watch.stop()
        directory.close()
        os.close(writer)
