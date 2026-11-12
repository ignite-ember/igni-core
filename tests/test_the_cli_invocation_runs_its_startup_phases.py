"""What ``igni`` does between parsing argv and running the session.

``CliInvocation`` owns resume lookup, the worktree, the trust carried into it,
the context handed to subcommands, reading the pipe, and the one-message run
under ``HeadlessRun``. The boundaries stubbed here are the session itself
(``SingleMessageRun``) and the session store behind ``--continue``; everything
the invocation decides is the real code.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import pytest

from igni_core.cli import invocation as invocation_module
from igni_core.cli.headless_run import HeadlessRun
from igni_core.cli.invocation import CliInvocation
from igni_core.cli.options import CliOptions
from igni_core.cli.resume import ResumeLookup
from igni_core.core.config.settings import Settings
from igni_core.core.workspace_trust import WorkspaceTrust, trust_service
from igni_core.core.workspace_trust.store import WorkspaceTrustStore


def _invocation(**options: Any) -> CliInvocation:
    return CliInvocation(CliOptions(**options), Settings(), HeadlessRun(drain_timeout_seconds=0.05))


# ── resume ──────────────────────────────────────────────────────────


@pytest.fixture
def latest(monkeypatch: pytest.MonkeyPatch):
    """The session store's answer to ``--continue``, and how often it was asked."""
    state: dict[str, Any] = {"answer": ResumeLookup(), "asked": 0}

    def latest_id(self) -> ResumeLookup:
        state["asked"] += 1
        return state["answer"]

    monkeypatch.setattr(invocation_module.ResumeResolver, "latest_id", latest_id)
    return state


def test_an_explicit_session_id_wins_and_nothing_is_looked_up(latest, capsys) -> None:
    latest["answer"] = ResumeLookup(session_id="older")
    inv = _invocation(session_id="chosen", continue_session=True)
    inv.resolve_resume_id()
    assert inv._resume_session_id == "chosen"
    assert latest["asked"] == 0
    assert capsys.readouterr().out == ""


def test_without_either_flag_there_is_no_resume_id(latest, capsys) -> None:
    inv = _invocation()
    inv.resolve_resume_id()
    assert inv._resume_session_id is None and latest["asked"] == 0
    assert capsys.readouterr().out == ""


def test_continue_resumes_the_latest_session_and_says_which(latest, capsys) -> None:
    latest["answer"] = ResumeLookup(session_id="s-42")
    inv = _invocation(continue_session=True)
    inv.resolve_resume_id()
    assert inv._resume_session_id == "s-42"
    assert capsys.readouterr().out == "Resuming last session: s-42\n"


def test_continue_with_no_sessions_says_so_and_starts_fresh(latest, capsys) -> None:
    inv = _invocation(continue_session=True)
    inv.resolve_resume_id()
    assert inv._resume_session_id is None
    assert capsys.readouterr().out == "No previous sessions found.\n"


def test_continue_when_the_lookup_fails_says_so_and_starts_fresh(latest, capsys) -> None:
    latest["answer"] = ResumeLookup(error="disk is gone")
    inv = _invocation(continue_session=True)
    inv.resolve_resume_id()
    assert inv._resume_session_id is None
    assert capsys.readouterr().out == "Could not look up last session.\n"


# ── worktree and trust ──────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": __import__("os").environ["PATH"],
        "HOME": str(tmp_path),
    }
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", *args], cwd=root, check=True, env=env)
    monkeypatch.chdir(root)
    monkeypatch.setattr("pathlib.Path.cwd", lambda: root)
    return root


def test_without_worktree_there_is_no_project_dir_and_nothing_is_printed(capsys) -> None:
    inv = _invocation()
    inv.setup_worktree()
    assert inv.project_dir is None
    assert capsys.readouterr().out == ""
    inv.cleanup_worktree()  # no worktree: nothing to report


def test_worktree_makes_a_real_git_worktree_and_names_it(repo: Path, capsys) -> None:
    inv = _invocation(worktree=True)
    inv.setup_worktree()
    made = inv.project_dir
    assert made is not None and made != repo and (made / ".git").exists()
    branch = subprocess.run(
        ["git", "-C", str(made), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert capsys.readouterr().out == f"Worktree: {made} (branch: {branch})\n"


def test_worktree_outside_a_repository_fails_with_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    bare = tmp_path / "plain"
    bare.mkdir()
    monkeypatch.chdir(bare)
    monkeypatch.setattr("pathlib.Path.cwd", lambda: bare)
    with pytest.raises(SystemExit) as raised:
        _invocation(worktree=True).setup_worktree()
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("worktree failed: ") and captured.out == ""


@pytest.fixture
def trust(tmp_path: Path):
    service = WorkspaceTrust(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))
    trust_service.replace(service)
    yield service
    trust_service.replace(None)


def test_a_trusted_folder_trusts_the_worktree_made_from_it(
    repo: Path, trust: WorkspaceTrust
) -> None:
    trust.trust(repo, remember=False)
    inv = _invocation(worktree=True)
    inv.setup_worktree()
    assert inv.project_dir is not None
    assert not trust.is_trusted(inv.project_dir)
    inv.carry_trust_into_worktree(repo)
    assert trust.is_trusted(inv.project_dir)


def test_an_untrusted_folder_leaves_its_worktree_untrusted(
    repo: Path, trust: WorkspaceTrust
) -> None:
    inv = _invocation(worktree=True)
    inv.setup_worktree()
    assert inv.project_dir is not None
    inv.carry_trust_into_worktree(repo)
    assert not trust.is_trusted(inv.project_dir)


def test_with_no_worktree_no_trust_is_granted(tmp_path: Path, trust: WorkspaceTrust) -> None:
    folder = tmp_path / "f"
    folder.mkdir()
    trust.trust(folder, remember=False)
    other = tmp_path / "g"
    other.mkdir()
    _invocation().carry_trust_into_worktree(folder)
    assert not trust.is_trusted(other)


# ── context ─────────────────────────────────────────────────────────


def test_add_dir_is_resolved_to_absolute_paths_and_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    assert _invocation().additional_dirs is None
    assert _invocation(add_dir=("link", "real/../real")).additional_dirs == [
        (tmp_path / "real").resolve(),
        (tmp_path / "real").resolve(),
    ]


def test_the_context_carries_settings_worktree_and_dirs(repo: Path) -> None:
    inv = _invocation(worktree=True, add_dir=("..",))
    inv.setup_worktree()
    ctx = click.Context(click.Command("igni"))
    inv.store_context(ctx)
    assert ctx.obj.settings is inv._settings
    assert ctx.obj.worktree_manager is inv._worktree_manager
    assert ctx.obj.project_dir == inv.project_dir
    assert ctx.obj.additional_dirs == [repo.parent.resolve()]


def test_the_context_without_a_worktree_has_no_project_dir() -> None:
    ctx = click.Context(click.Command("igni"))
    _invocation().store_context(ctx)
    assert ctx.obj.worktree_manager is None and ctx.obj.project_dir is None
    assert ctx.obj.additional_dirs is None


# ── the pipe ────────────────────────────────────────────────────────


class _Stdin(io.StringIO):
    def __init__(self, text: str, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _read(
    monkeypatch: pytest.MonkeyPatch, text: str, message: str | None, tty: bool = False
) -> str:
    monkeypatch.setattr(sys, "stdin", _Stdin(text, tty))
    return _invocation(message=message).read_pipe_message()


def test_stdin_alone_is_the_message_stripped(monkeypatch) -> None:
    assert _read(monkeypatch, "  what is 2+2?\n\n", None) == "what is 2+2?"


def test_the_message_and_stdin_are_joined_by_a_blank_line(monkeypatch) -> None:
    assert _read(monkeypatch, "data\n", "summarize") == "summarize\n\ndata"


@pytest.mark.parametrize("text", ["", "  \n\t\n"])
def test_the_message_alone_is_used_when_stdin_is_empty(monkeypatch, text) -> None:
    assert _read(monkeypatch, text, "just this") == "just this"


@pytest.mark.parametrize("text", ["", "  \n\t\n"])
def test_no_message_and_no_stdin_exits_one_with_an_error(monkeypatch, capsys, text) -> None:
    with pytest.raises(SystemExit) as raised:
        _read(monkeypatch, text, None)
    assert raised.value.code == 1
    assert capsys.readouterr().err == "Error: no input provided via stdin or -m\n"


# ── running the session ─────────────────────────────────────────────


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, Any] = {}

    class FakeRun:
        def __init__(self, settings, **kwargs) -> None:
            seen["settings"], seen["kwargs"] = settings, kwargs

        async def run(self, message: str) -> None:
            seen["message"] = message
            if seen.get("fail"):
                raise RuntimeError("model unreachable")

    monkeypatch.setattr(invocation_module, "SingleMessageRun", FakeRun)
    return seen


def test_a_message_runs_one_session_with_the_resume_id_and_dirs(
    session, latest, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    latest["answer"] = ResumeLookup(session_id="s-7")
    inv = _invocation(continue_session=True, add_dir=("extra",))
    inv.resolve_resume_id()
    inv.run_message("hello")
    assert session["message"] == "hello"
    assert session["kwargs"] == {
        "resume_session_id": "s-7",
        "project_dir": None,
        "additional_dirs": [(tmp_path / "extra").resolve()],
    }
    assert session["settings"] is inv._settings


def test_pipe_mode_runs_what_was_read(session, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin("from the pipe", False))
    _invocation(message="do:").run_pipe()
    assert session["message"] == "do:\n\nfrom the pipe"


def test_a_failing_session_exits_one_and_still_reports_the_worktree_cleanup(
    session, repo: Path, capsys
) -> None:
    session["fail"] = True
    inv = _invocation(worktree=True)
    inv.setup_worktree()
    capsys.readouterr()
    with pytest.raises(SystemExit) as raised:
        inv.run_message("go")
    assert raised.value.code == 1
    assert "model unreachable" in capsys.readouterr().err


def test_the_help_pointer_is_printed(capsys) -> None:
    _invocation().echo_help_pointer()
    assert capsys.readouterr().out == invocation_module._HELP_TEXT + "\n"
