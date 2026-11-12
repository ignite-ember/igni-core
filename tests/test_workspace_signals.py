"""The producer of ``host.workspace.*`` signals: its cap, its model, its poll.

The rules of what an extension hears live in
``test_the_host_says_what_it_knows.py``. These pin the producer's own
behaviour: the cap on a path list, what it does before a model is
attached, and that start and stop run the poll over a real host model.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from igni_core.backend.workspace_signals import WorkspaceSignals
from igni_core.backend.workspace_watch import FileStamp, Head
from igni_core.extensions.events import HostTopic
from tests.host_model_helpers import HostRig, Sink

FILES = HostTopic.WORKSPACE_FILES_CHANGED.value
HEAD = HostTopic.WORKSPACE_HEAD_CHANGED.value


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[HostRig]:
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


class _Workspace:
    """The git state the poll reads, set by the test."""

    def __init__(self) -> None:
        self.head_value: Head | None = Head(commit_sha="aaa", branch="main")
        self.dirty_value: dict[str, FileStamp] | None = {}
        self.reads = 0

    def head(self) -> Head | None:
        self.reads += 1
        return self.head_value

    def dirty(self) -> dict[str, FileStamp] | None:
        self.reads += 1
        return self.dirty_value


def _signals(project: Path, workspace: _Workspace) -> WorkspaceSignals:
    signals = WorkspaceSignals(project_dir=project)
    signals._watcher._workspace = workspace  # type: ignore[assignment]  # noqa: SLF001
    signals._watcher._head_interval = 0.01  # noqa: SLF001
    signals._watcher._files_interval = 0.0  # noqa: SLF001
    return signals


def _listen(rig: HostRig, *wires: str) -> Sink:
    assert rig.model is not None
    sink = Sink()
    bus = rig.model.services.bus
    bus.subscribe(bus.attach("acme.sees", sink), list(wires))
    return sink


def test_the_cap_is_five_hundred_paths() -> None:
    assert WorkspaceSignals.MAX_PATHS == 500


async def test_six_hundred_changed_paths_reach_a_listener_as_five_hundred_marked_partial(
    rig: HostRig,
) -> None:
    rig.install("acme.sees", workspace="read")
    heard = _listen(rig, FILES)
    signals = WorkspaceSignals(project_dir=rig.project)
    signals.attach_model(rig.model)

    signals.files_changed(frozenset(f"src/f{n:04d}.py" for n in range(600)))

    [value] = heard.values(FILES)
    assert value["truncated"] is True
    assert value["paths"] == [f"src/f{n:04d}.py" for n in range(500)]


async def test_exactly_five_hundred_paths_are_not_marked_partial(rig: HostRig) -> None:
    rig.install("acme.sees", workspace="read")
    heard = _listen(rig, FILES)
    signals = WorkspaceSignals(project_dir=rig.project)
    signals.attach_model(rig.model)

    signals.files_changed(frozenset(f"src/f{n:04d}.py" for n in range(500)))

    [value] = heard.values(FILES)
    assert value["truncated"] is False
    assert len(value["paths"]) == 500


async def test_with_no_model_nothing_is_wanted_nothing_is_delivered_and_no_git_is_read(
    tmp_path: Path,
) -> None:
    workspace = _Workspace()
    signals = _signals(tmp_path, workspace)

    signals.head_changed(Head(commit_sha="aaa", branch="main"))
    signals.files_changed(frozenset({"a.py"}))
    await signals.poll_once()
    await signals.poll_once()

    assert not signals._wanted(HostTopic.WORKSPACE_HEAD_CHANGED)  # noqa: SLF001
    assert not signals._wanted(HostTopic.WORKSPACE_FILES_CHANGED)  # noqa: SLF001
    assert workspace.reads == 0


async def test_a_model_attached_after_construction_is_the_one_told(rig: HostRig) -> None:
    rig.install("acme.sees", workspace="read")
    heard = _listen(rig, HEAD)
    signals = WorkspaceSignals(project_dir=rig.project)
    signals.head_changed(Head(commit_sha="early", branch="main"))  # no model yet: dropped

    signals.attach_model(rig.model)
    signals.head_changed(Head(commit_sha="late", branch="dev"))

    assert heard.values(HEAD) == [{"commit_sha": "late", "branch": "dev"}]


async def test_start_polls_for_a_listener_and_stop_ends_the_poll(rig: HostRig) -> None:
    rig.install("acme.sees", workspace="read")
    heard = _listen(rig, HEAD, FILES)
    workspace = _Workspace()
    signals = _signals(rig.project, workspace)
    signals.attach_model(rig.model)

    await signals.start()
    await signals.start()  # a second start does not run a second poll
    await _until(lambda: workspace.reads >= 2)  # baseline learned
    workspace.head_value = Head(commit_sha="bbb", branch="main")
    workspace.dirty_value = {"src/a.py": FileStamp(mtime_ns=1, size=1)}
    await _until(lambda: heard.values(HEAD))
    await _until(lambda: heard.values(FILES))
    await signals.stop()

    assert heard.values(HEAD) == [{"commit_sha": "bbb", "branch": "main"}]
    assert heard.values(FILES)[0]["paths"] == ["src/a.py"]
    settled = workspace.reads
    await asyncio.sleep(0.1)
    assert workspace.reads == settled


async def test_stop_before_start_is_a_no_op(tmp_path: Path) -> None:
    await WorkspaceSignals(project_dir=tmp_path).stop()


async def _until(condition) -> None:
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition not reached")
