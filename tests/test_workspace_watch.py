"""The workspace watcher and its git reader, in the corners the host tests skip.

``tests/test_the_host_says_what_it_knows.py`` pins the event rules with a fake
workspace and the happy path of the git reader. This is the rest: what the
reader says when git is missing, slow, or answers oddly, what a path with a
space or an accent comes back as, how the watcher keeps its baseline across a
failed look, and the lifecycle of the loop that drives it.
"""

from __future__ import annotations

import asyncio
import stat
import subprocess
from pathlib import Path

import pytest

from igni_core.backend.workspace_watch import (
    FileStamp,
    GitWorkspace,
    Head,
    WorkspaceWatcher,
)
from igni_core.extensions.events import HostTopic


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.py").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "first")
    return tmp_path


def _fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> Path:
    """Put a `git` that runs ``script`` first on PATH; returns a project dir."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    exe = bin_dir / "git"
    exe.write_text("#!/bin/sh\n" + script + "\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    project = tmp_path / "proj"
    project.mkdir()
    return project


# ── The reader when git is not what it expects ─────────────────────


def test_a_repository_with_no_commits_has_no_head(tmp_path: Path):
    _git(tmp_path, "init", "-b", "main")
    assert GitWorkspace(tmp_path).head() is None


def test_a_detached_head_reports_the_branch_as_head(repo: Path):
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(repo, "checkout", "--detach")
    head = GitWorkspace(repo).head()
    assert head == Head(commit_sha=sha, branch="HEAD")
    assert len(head.commit_sha) == 40


def test_a_missing_git_is_none_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    workspace = GitWorkspace(tmp_path)
    assert workspace.head() is None
    assert workspace.dirty() is None


def test_a_git_that_hangs_is_none_after_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = _fake_git(tmp_path, monkeypatch, "/bin/sleep 5")
    workspace = GitWorkspace(project)
    workspace._TIMEOUT_SECONDS = 0.2  # noqa: SLF001 — the timeout is the subject
    assert workspace.head() is None
    assert workspace.dirty() is None


def test_a_one_line_answer_is_not_a_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _fake_git(tmp_path, monkeypatch, "echo abc123")
    assert GitWorkspace(project).head() is None


def test_a_blank_commit_line_is_not_a_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _fake_git(tmp_path, monkeypatch, "echo '  '; echo main")
    assert GitWorkspace(project).head() is None


def test_head_is_read_in_one_git_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log = tmp_path / "calls"
    project = _fake_git(tmp_path, monkeypatch, f"echo x >> {log}; echo deadbeef; echo feature/x")
    assert GitWorkspace(project).head() == Head(commit_sha="deadbeef", branch="feature/x")
    assert log.read_text().count("x") == 1


def test_a_status_record_too_short_to_hold_a_path_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = _fake_git(tmp_path, monkeypatch, "printf '?? \\0 M a.py\\0'")
    (project / "a.py").write_text("hi")
    dirty = GitWorkspace(project).dirty()
    assert dirty is not None
    assert set(dirty) == {"a.py"}


def test_a_failing_git_status_is_none_not_an_empty_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = _fake_git(tmp_path, monkeypatch, "echo 'fatal' >&2; exit 128")
    assert GitWorkspace(project).dirty() is None


def test_paths_with_spaces_and_accents_come_back_as_written(repo: Path):
    (repo / "my file.txt").write_text("1")
    (repo / "café.txt").write_text("22")
    dirty = GitWorkspace(repo).dirty()
    assert dirty is not None
    assert dirty["my file.txt"].size == 1
    assert dirty["café.txt"].size == 2


def test_a_clean_repository_has_an_empty_dirty_set(repo: Path):
    assert GitWorkspace(repo).dirty() == {}


# ── The watcher keeps its baseline across a look that failed ───────


class _Workspace:
    def __init__(self) -> None:
        self.head_value: Head | None = Head("aaa", "main")
        self.dirty_value: dict[str, FileStamp] | None = {}
        self.head_calls = 0

    def head(self) -> Head | None:
        self.head_calls += 1
        return self.head_value

    def dirty(self) -> dict[str, FileStamp] | None:
        return self.dirty_value


def _watch(workspace, wants=lambda _topic: True, **kwargs):
    heads: list[Head] = []
    files: list[frozenset[str]] = []
    watch = WorkspaceWatcher(
        workspace=workspace,
        on_head=heads.append,
        on_files=files.append,
        wants=wants,
        files_interval_seconds=0.0,
        **kwargs,
    )
    return watch, heads, files


async def test_a_look_that_fails_keeps_the_head_baseline():
    workspace = _Workspace()
    watch, heads, _ = _watch(workspace)
    await watch.poll_once()
    workspace.head_value = None
    await watch.poll_once()
    workspace.head_value = Head("aaa", "main")
    await watch.poll_once()
    assert heads == []
    workspace.head_value = None
    await watch.poll_once()
    workspace.head_value = Head("bbb", "main")
    await watch.poll_once()
    assert heads == [Head("bbb", "main")]


async def test_a_look_that_fails_keeps_the_files_baseline():
    workspace = _Workspace()
    watch, _, files = _watch(workspace)
    await watch.poll_once()
    workspace.dirty_value = None
    await watch.poll_once()
    workspace.dirty_value = {"a.py": FileStamp(1, 1)}
    await watch.poll_once()
    assert files == [frozenset({"a.py"})]


async def test_a_file_listener_leaving_drops_the_files_baseline():
    workspace = _Workspace()
    listening = {"on": True}
    watch, _, files = _watch(
        workspace,
        wants=lambda topic: listening["on"] and topic is HostTopic.WORKSPACE_FILES_CHANGED,
    )
    await watch.poll_once()
    listening["on"] = False
    await watch.poll_once()
    workspace.dirty_value = {"a.py": FileStamp(1, 1)}
    listening["on"] = True
    await watch.poll_once()
    assert files == []


async def test_only_the_head_topic_means_no_status_call():
    workspace = _Workspace()
    seen: list[str] = []
    workspace.dirty = lambda: seen.append("dirty")  # type: ignore[method-assign]
    watch, _, _ = _watch(workspace, wants=lambda topic: topic is HostTopic.WORKSPACE_HEAD_CHANGED)
    await watch.poll_once()
    assert seen == []
    assert workspace.head_calls == 1


def test_the_default_cadences_and_their_overrides():
    assert WorkspaceWatcher.HEAD_INTERVAL_SECONDS == 1.0
    assert WorkspaceWatcher.FILES_INTERVAL_SECONDS == 2.0
    assert WorkspaceWatcher.IDLE_INTERVAL_SECONDS == 60.0
    watch, _, _ = _watch(_Workspace(), head_interval_seconds=0.25)
    assert watch._head_interval == 0.25  # noqa: SLF001
    assert watch._files_interval == 0.0  # noqa: SLF001
    default = WorkspaceWatcher(
        workspace=_Workspace(),  # type: ignore[arg-type]
        on_head=lambda _h: None,
        on_files=lambda _f: None,
        wants=lambda _t: True,
    )
    assert default._head_interval == 1.0  # noqa: SLF001
    assert default._files_interval == 2.0  # noqa: SLF001


# ── The real reader wired to the real watcher ──────────────────────


async def test_a_real_repository_reports_a_commit_and_an_edit(repo: Path):
    watch, heads, files = _watch(GitWorkspace(repo))
    await watch.poll_once()
    assert heads == [] and files == []
    (repo / "b.py").write_text("new\n")
    await watch.poll_once()
    assert files == [frozenset({"b.py"})]
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-m", "second")
    await watch.poll_once()
    assert len(heads) == 1
    assert heads[0].branch == "main"
    assert files[-1] == frozenset({"b.py"})


# ── The loop's lifecycle ───────────────────────────────────────────


async def test_start_runs_one_named_daemon_and_a_second_start_adds_none():
    watch, _, _ = _watch(_Workspace())
    await watch.start()
    try:
        first = watch._task  # noqa: SLF001
        assert first is not None
        assert first.get_name() == "workspace-watch"
        await watch.start()
        assert watch._task is first  # noqa: SLF001
    finally:
        await watch.stop()
    assert first.done()


async def test_stop_before_start_and_stop_twice_are_safe():
    watch, _, _ = _watch(_Workspace())
    await watch.stop()
    await watch.start()
    await watch.stop()
    await watch.stop()
    assert watch._task is None  # noqa: SLF001


async def test_a_stopped_watcher_can_be_started_again():
    workspace = _Workspace()
    watch, _, _ = _watch(workspace, head_interval_seconds=0.01)
    await watch.start()
    await watch.stop()
    await watch.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if workspace.head_calls:
                break
    finally:
        await watch.stop()
    assert workspace.head_calls >= 1


async def test_a_poll_that_raises_does_not_end_the_loop():
    workspace = _Workspace()
    state = {"first": True}
    original = workspace.head

    def flaky() -> Head | None:
        if state["first"]:
            state["first"] = False
            raise RuntimeError("git went away")
        return original()

    workspace.head = flaky  # type: ignore[method-assign]
    watch, heads, _ = _watch(workspace, head_interval_seconds=0.01)
    await watch.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if workspace.head_calls:
                break
        workspace.head_value = Head("bbb", "main")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if heads:
                break
    finally:
        await watch.stop()
    assert heads == [Head("bbb", "main")]


async def test_with_nobody_listening_the_loop_sleeps_the_idle_interval():
    workspace = _Workspace()
    listening = {"on": False}
    watch, _, _ = _watch(
        workspace, wants=lambda _topic: listening["on"], head_interval_seconds=0.01
    )
    await watch.start()
    try:
        await asyncio.sleep(0.05)
        listening["on"] = True
        await asyncio.sleep(0.15)
    finally:
        await watch.stop()
    # Still inside the first (idle, 60 s) sleep: a subscriber is not polled for
    # at the head cadence until the idle sleep ends.
    assert workspace.head_calls == 0
