"""A spawn's sandbox tells the parent what became of the worktree.

``SpawnSandbox.finalize`` turns every way ``WorktreeManager.cleanup``
can end into the footer the parent agent reads: reaped, preserved with
the commands to merge or remove it, or failed with git's own stderr —
a failed cleanup used to read as success. ``create`` expresses a
worktree that cannot be made as a ``SandboxSetupResult`` with an
``error``, never as an exception.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import igni_core.core.tools.orchestrate_sandbox as sandbox_module
from igni_core.core.tools.orchestrate_modes import IsolationMode
from igni_core.core.tools.orchestrate_sandbox import SandboxSetupResult, SpawnSandbox
from igni_core.core.worktree import WorktreeCleanupResult, WorktreeCreateResult, WorktreeInfo


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)


def _info() -> WorktreeInfo:
    return WorktreeInfo(worktree_path=Path("/wt/x"), branch_name="br-x", original_dir=Path("/repo"))


def _sandbox_ending_with(result: WorktreeCleanupResult) -> SpawnSandbox:
    manager = MagicMock()
    manager.cleanup.return_value = result
    return SpawnSandbox(manager=manager, info=_info(), original_base_dirs={}, task="")


def _create(project_dir, **overrides):
    kwargs = {
        "project_dir": project_dir,
        "session_id": "s1",
        "agent": SimpleNamespace(tools=[]),
        "agent_name": "editor",
        "isolation": IsolationMode.WORKTREE,
        "task": "do it",
    }
    kwargs.update(overrides)
    return SpawnSandbox.create(**kwargs)


class TestTheFooterSaysWhatCleanupDid:
    def test_a_clean_worktree_is_reaped(self):
        footer = _sandbox_ending_with(WorktreeCleanupResult(status="cleaned")).finalize()

        assert footer == "\n\nWorktree br-x (clean) — reaped."

    def test_a_dirty_worktree_is_preserved_with_the_commands_to_deal_with_it(self):
        footer = _sandbox_ending_with(WorktreeCleanupResult(status="preserved_dirty")).finalize()

        assert footer == (
            "\n\nWorktree preserved: /wt/x (branch: br-x) — has uncommitted changes.\n"
            "To merge: git merge br-x\n"
            "To remove: git worktree remove /wt/x"
        )

    @pytest.mark.parametrize("status", ["git_remove_failed", "branch_delete_failed"])
    def test_a_failed_cleanup_surfaces_its_status_and_stderr(self, status):
        """It used to read as a success; the parent has to see it failed."""
        footer = _sandbox_ending_with(
            WorktreeCleanupResult(status=status, stderr="fatal: locked")
        ).finalize()

        assert footer == (f"\n\nWorktree: /wt/x (branch: br-x) — cleanup {status}: fatal: locked")
        assert "reaped" not in footer

    def test_a_cleanup_that_raises_is_a_footer_not_an_exception(self):
        manager = MagicMock()
        manager.cleanup.side_effect = RuntimeError("boom")
        sandbox = SpawnSandbox(manager=manager, info=_info(), original_base_dirs={}, task="")

        assert sandbox.finalize() == ("\n\nWorktree: /wt/x (branch: br-x) — cleanup failed: boom")


class TestFinalizeAgainstARealWorktree:
    def test_a_dirty_worktree_is_kept_on_disk(self, tmp_path):
        _init_git_repo(tmp_path)
        sandbox = _create(tmp_path).sandbox
        assert sandbox is not None and sandbox.info is not None
        (sandbox.info.worktree_path / "new.txt").write_text("work")

        footer = sandbox.finalize()

        assert "Worktree preserved:" in footer
        assert f"To merge: git merge {sandbox.info.branch_name}" in footer
        assert (sandbox.info.worktree_path / "new.txt").exists()


class TestCreateReportsExpectedFailures:
    def test_a_create_that_is_not_ok_is_an_error_result(self, monkeypatch, tmp_path):
        manager = MagicMock()
        manager.create_result.return_value = WorktreeCreateResult(
            ok=False, error="git_add_failed", message="Failed to create worktree: nope"
        )
        monkeypatch.setattr(sandbox_module, "WorktreeManager", lambda _dir: manager)

        setup = _create(tmp_path)

        assert setup.sandbox is None
        assert setup.error == "Error: worktree create failed — Failed to create worktree: nope"

    def test_an_ok_result_without_info_is_an_error_result(self, monkeypatch, tmp_path):
        manager = MagicMock()
        manager.create_result.return_value = WorktreeCreateResult(ok=True, info=None, message="m")
        monkeypatch.setattr(sandbox_module, "WorktreeManager", lambda _dir: manager)

        setup = _create(tmp_path)

        assert setup.sandbox is None
        assert setup.error == "Error: worktree create failed — m"

    def test_no_project_dir_is_refused_by_name(self):
        assert _create(None).error == "Error: isolation=worktree requires a project directory."

    def test_a_directory_that_is_not_a_repository_is_refused(self, tmp_path):
        setup = _create(tmp_path)

        assert setup.sandbox is None
        assert setup.error is not None
        assert setup.error.startswith("Error: cannot create worktree — ")


class TestTheWorktreeName:
    def _suffix_of(self, monkeypatch, session_id: str) -> str:
        manager = MagicMock()
        manager.create_result.return_value = WorktreeCreateResult(ok=True, info=_info(), message="")
        monkeypatch.setattr(sandbox_module, "WorktreeManager", lambda _dir: manager)
        _create(Path("."), session_id=session_id)
        return manager.create_result.call_args.kwargs["session_id"]

    def test_it_starts_with_the_first_eight_characters_of_the_session(self, monkeypatch):
        suffix = self._suffix_of(monkeypatch, "0123456789abcdef")

        head, _, tail = suffix.partition("-editor-")
        assert head == "01234567"
        assert len(tail) == 6

    def test_an_empty_session_id_falls_back_to_sess(self, monkeypatch):
        assert self._suffix_of(monkeypatch, "").startswith("sess-editor-")

    def test_two_spawns_of_one_agent_get_different_names(self, monkeypatch):
        assert self._suffix_of(monkeypatch, "s1") != self._suffix_of(monkeypatch, "s1")


class TestRebindEdges:
    def test_a_toolkit_that_cannot_be_copied_leaves_the_agent_unisolated(self):
        class _Uncopyable:
            base_dir = Path("/orig")

            def __copy__(self):
                raise TypeError("no copy")

        tool = _Uncopyable()
        agent = SimpleNamespace(tools=[tool])

        assert SpawnSandbox.rebind_tool_base_dirs(agent, Path("/wt")) == {}
        assert agent.tools == [tool]
        assert tool.base_dir == Path("/orig")

    def test_a_base_dir_that_cannot_be_set_does_not_stop_the_other_tools(self):
        class _Frozen:
            def __init__(self):
                self._dir = Path("/frozen")

            @property
            def base_dir(self):
                return self._dir

            @base_dir.setter
            def base_dir(self, _value):
                raise AttributeError("read only")

        class _Plain:
            base_dir = Path("/plain")

        agent = SimpleNamespace(tools=[_Frozen(), _Plain()])

        SpawnSandbox.rebind_tool_base_dirs(agent, Path("/wt"))

        assert agent.tools[0].base_dir == Path("/frozen")
        assert agent.tools[1].base_dir == Path("/wt")

    def test_tools_set_to_none_are_left_alone(self):
        agent = SimpleNamespace(tools=None)

        assert SpawnSandbox.rebind_tool_base_dirs(agent, Path("/wt")) == {}
        assert agent.tools is None


def test_a_setup_result_starts_empty():
    result = SandboxSetupResult()

    assert result.sandbox is None
    assert result.task == ""
    assert result.error is None
