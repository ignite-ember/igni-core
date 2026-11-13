"""MemorySyncHook: run the tool, hand back its result, then mirror the memory directory.

The mirror is the real one; the stored side is a plain directory so no
database is needed.
"""

from __future__ import annotations

import inspect
import threading
from pathlib import Path

import pytest

from igni_core.core.hooks.memory_sync import MemorySyncHook
from igni_core.core.utils.memory_files import DirectoryMemoryFiles, MemoryMirror


class _WatchedMirror(MemoryMirror):
    """The real mirror, noting how many syncs ran and on which thread."""

    def __init__(self, files: DirectoryMemoryFiles, directory: Path) -> None:
        super().__init__(files, directory)
        self.sync_threads: list[int] = []

    def sync(self) -> int:
        self.sync_threads.append(threading.get_ident())
        return super().sync()


@pytest.fixture
def stored(tmp_path: Path) -> DirectoryMemoryFiles:
    return DirectoryMemoryFiles(tmp_path / "stored")


@pytest.fixture
def mirror(tmp_path: Path, stored: DirectoryMemoryFiles) -> _WatchedMirror:
    directory = tmp_path / "memory"
    directory.mkdir()
    return _WatchedMirror(stored, directory)


def test_the_hook_is_marked_as_a_coroutine_function_for_agno(mirror):
    assert inspect.iscoroutinefunction(MemorySyncHook(mirror))


async def test_a_sync_tool_runs_with_its_args_and_its_file_is_stored_before_the_hook_returns(
    mirror, stored
):
    def save(file_name: str, contents: str) -> str:
        (mirror.directory / file_name).write_text(contents)
        return "saved"

    result = await MemorySyncHook(mirror)(
        name="save", func=save, args={"file_name": "a.md", "contents": "likes tea"}
    )

    assert result == "saved"
    assert stored.read("a.md") == "likes tea"


async def test_an_async_tool_is_awaited_and_its_result_returned_unchanged(mirror, stored):
    async def save(file_name: str) -> dict[str, str]:
        (mirror.directory / file_name).write_text("x")
        return {"ok": "yes"}

    result = await MemorySyncHook(mirror)(name="save", func=save, args={"file_name": "b.md"})

    assert result == {"ok": "yes"}
    assert stored.read("b.md") == "x"


async def test_a_falsy_result_is_returned_as_it_is(mirror):
    assert await MemorySyncHook(mirror)(name="t", func=lambda: 0, args={}) == 0
    assert await MemorySyncHook(mirror)(name="t", func=lambda: "", args={}) == ""


async def test_a_file_written_by_a_shell_style_tool_is_mirrored_too(mirror, stored):
    def shell() -> str:
        (mirror.directory / "from-shell.md").write_text("echoed")
        return "done"

    await MemorySyncHook(mirror)(name="run_shell_command", func=shell, args={})

    assert stored.names() == ["from-shell.md"]


async def test_no_func_returns_none_and_still_syncs(mirror, stored):
    (mirror.directory / "pre.md").write_text("already there")

    result = await MemorySyncHook(mirror)(name="x", func=None, args={"ignored": 1})

    assert result is None
    assert len(mirror.sync_threads) == 1
    assert stored.read("pre.md") == "already there"


async def test_args_none_calls_the_tool_with_no_arguments(mirror):
    seen: list[dict[str, object]] = []

    def tool(**kwargs: object) -> str:
        seen.append(kwargs)
        return "r"

    assert await MemorySyncHook(mirror)(name="t", func=tool, args=None) == "r"
    assert await MemorySyncHook(mirror)(name="t", func=tool) == "r"
    assert seen == [{}, {}]


async def test_extra_keywords_from_agno_are_ignored(mirror):
    result = await MemorySyncHook(mirror)(
        name="t", func=lambda: "r", args={}, agent=object(), function_call=object()
    )

    assert result == "r"


async def test_a_tool_that_raises_propagates_and_skips_the_sync(mirror, stored):
    def broken() -> None:
        (mirror.directory / "half.md").write_text("written before the failure")
        raise RuntimeError("tool failed")

    with pytest.raises(RuntimeError, match="tool failed"):
        await MemorySyncHook(mirror)(name="t", func=broken, args={})

    assert mirror.sync_threads == []
    assert stored.names() == []


async def test_a_failing_async_tool_propagates_and_skips_the_sync(mirror):
    async def broken() -> None:
        raise ValueError("async tool failed")

    with pytest.raises(ValueError, match="async tool failed"):
        await MemorySyncHook(mirror)(name="t", func=broken, args={})

    assert mirror.sync_threads == []


async def test_the_blocking_sync_runs_off_the_event_loop_thread(mirror):
    await MemorySyncHook(mirror)(name="t", func=lambda: None, args={})

    assert len(mirror.sync_threads) == 1
    assert mirror.sync_threads[0] != threading.get_ident()
