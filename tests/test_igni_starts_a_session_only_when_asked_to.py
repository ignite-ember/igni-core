"""When bare ``igni`` does something, and when a subcommand has it to itself.

``igni`` is a group so that ``igni ext`` and ``igni login`` exist, and it
runs without a subcommand so that ``igni`` and ``igni -m "..."`` keep their
meaning. What that must not become is a CLI that does its own setup on the
way to a subcommand, or one that starts a session nobody asked for.

The real CLI is run through Click's runner. Settings loading and the
session itself are the only things stood in for, and each test says which.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import igni_core.cli as cli_module
from igni_core import __version__
from igni_core.cli.headless_run import HeadlessRun
from igni_core.cli.options import CliOptions
from igni_core.cli.run_settings import CliSettingsLoader
from igni_core.core.agno_telemetry import AgnoTelemetry
from igni_core.core.air_gap import AIR_GAPPED_ENV
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.settings import Settings
from igni_core.core.observability import OtelRuntime
from igni_core.core.worktree import WorktreeManager


@pytest.fixture(autouse=True)
def _environment_is_put_back(monkeypatch: pytest.MonkeyPatch):
    """The CLI writes into ``os.environ`` (the air-gap decision, Agno's
    telemetry switch), as it must for the processes it starts; the next
    test starts from what this one found."""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


class Session:
    """Stands in for the session a run would start, and records the asks."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def run(self, coroutine: Any, *, cleanup: Any = None) -> None:
        coroutine.close()
        self.messages.append("started")


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Session:
    seen = Session()
    monkeypatch.setattr(HeadlessRun, "run", lambda _self, coro, **kw: seen.run(coro, **kw))
    return seen


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[CliOptions]:
    """Every ``CliSettingsLoader.load`` the CLI makes, with default settings."""
    calls: list[CliOptions] = []

    def load(self: CliSettingsLoader) -> Settings:
        calls.append(self._options)  # type: ignore[attr-defined]
        return Settings()

    monkeypatch.setattr(CliSettingsLoader, "load", load)
    return calls


def _invoke(*argv: str, **kwargs: Any):
    return CliRunner().invoke(cli_module.cli, list(argv), **kwargs)


# ── what the group is ──────────────────────────────────────────────


def test_the_subcommands_are_ext_login_migrate_and_trust():
    assert sorted(cli_module.cli.commands) == ["ext", "login", "migrate", "trust"]


def test_version_names_igni_and_the_package_version():
    result = _invoke("--version")
    assert result.exit_code == 0
    assert result.output.strip() == f"igni, version {__version__}"


def test_help_lists_the_subcommands_and_none_of_the_implementation():
    result = _invoke("--help", prog_name="igni")
    assert result.exit_code == 0
    assert "Usage: igni [OPTIONS] [COMMAND] [ARGS]..." in result.output
    for command in ("ext", "login", "migrate", "trust"):
        assert f"\n  {command} " in result.output
    assert "igni — an AI coding assistant that works on your code where it lives." in result.output
    assert "Run one message with -m" in result.output
    for leaked in (":class:", "CliInvocation", "CliOptions", "decorator-per-option", "Click"):
        assert leaked not in result.output


# ── a subcommand has the command to itself ─────────────────────────


def test_a_subcommand_runs_without_the_clis_own_setup(tmp_path, loads, session):
    result = _invoke("ext", "list", "--root", str(tmp_path / "store"))
    assert result.exit_code == 0, result.output
    assert f"nothing installed in {tmp_path / 'store'}" in result.output
    assert loads == [], "settings were loaded for a command that reads its own"
    assert session.messages == []
    # And the help pointer a bare ``igni`` prints is not printed on top of it.
    assert "no longer ships a terminal chat UI" not in result.output


def test_a_subcommand_still_runs_when_group_policy_is_required_and_unavailable(
    tmp_path, monkeypatch
):
    """``igni login`` is how a person gets out of that state."""

    def unavailable(self: CliSettingsLoader) -> Settings:
        raise GroupPolicyUnavailable("no verified pack")

    monkeypatch.setattr(CliSettingsLoader, "load", unavailable)
    result = _invoke("ext", "list", "--root", str(tmp_path / "s"))
    assert result.exit_code == 0, result.output


# ── no subcommand ──────────────────────────────────────────────────


def test_bare_igni_points_at_the_clients_and_starts_no_session(loads, session):
    result = _invoke()
    assert result.exit_code == 0, result.output
    assert "no longer ships a terminal chat UI" in result.output
    assert 'igni -m "<message>"' in result.output
    assert "python -m igni_core.backend" in result.output
    assert len(loads) == 1
    assert session.messages == []


def test_an_empty_message_is_no_message(loads, session):
    result = _invoke("-m", "")
    assert result.exit_code == 0, result.output
    assert "no longer ships a terminal chat UI" in result.output
    assert session.messages == []


def test_a_message_starts_one_session_and_prints_no_pointer(loads, session, monkeypatch):
    from igni_core.cli import trust

    monkeypatch.setattr(trust.SessionTrust, "settle", lambda self: False)
    result = _invoke("-m", "hello")
    assert result.exit_code == 0, result.output
    assert session.messages == ["started"]
    assert "no longer ships a terminal chat UI" not in result.output


def test_a_required_policy_with_no_verified_pack_ends_in_its_message(monkeypatch, session):
    def unavailable(self: CliSettingsLoader) -> Settings:
        raise GroupPolicyUnavailable("group policy is required and no verified pack is on disk")

    monkeypatch.setattr(CliSettingsLoader, "load", unavailable)
    result = _invoke("-m", "hello")
    assert result.exit_code == 1
    assert (
        result.output.strip() == "Error: group policy is required and no verified pack is on disk"
    )
    assert isinstance(result.exception, SystemExit)
    assert session.messages == []


# ── workspace trust ────────────────────────────────────────────────


def _trust_that(monkeypatch: pytest.MonkeyPatch, answer: bool) -> list[dict[str, Any]]:
    """Make the folder's trust settle as ``answer``; return how it was asked."""
    from igni_core.cli import trust

    asked: list[dict[str, Any]] = []

    def settle(self: Any) -> bool:
        asked.append({"pipe": self._pipe, "flag": self._trust_flag, "quiet": self._quiet})
        return answer

    monkeypatch.setattr(trust.SessionTrust, "settle", settle)
    return asked


def test_a_folder_that_became_trusted_reloads_the_settings_with_its_config(
    monkeypatch, loads, session
):
    asked = _trust_that(monkeypatch, True)
    result = _invoke("-m", "hello", "--trust", "--quiet")
    assert result.exit_code == 0, result.output
    assert len(loads) == 2
    assert asked == [{"pipe": False, "flag": True, "quiet": True}]


def test_a_folder_that_stayed_untrusted_keeps_the_first_settings(monkeypatch, loads, session):
    _trust_that(monkeypatch, False)
    _invoke("-m", "hello")
    assert len(loads) == 1


def test_a_pipe_run_settles_trust_as_a_pipe(monkeypatch, loads, session):
    asked = _trust_that(monkeypatch, False)
    result = _invoke("-p", input="from stdin")
    assert result.exit_code == 0, result.output
    assert asked == [{"pipe": True, "flag": False, "quiet": False}]
    assert session.messages == ["started"]


def test_the_help_pointer_never_asks_about_trust(monkeypatch, loads, session):
    asked = _trust_that(monkeypatch, True)
    _invoke()
    assert asked == []
    assert len(loads) == 1


# ── what the run exports before it starts ──────────────────────────


def _watch_start(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """The environment as it stands when observability starts, which is
    after the air-gap decision and the telemetry rule were applied."""
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(
        OtelRuntime, "start", classmethod(lambda cls, s: seen.append(dict(os.environ)))
    )
    return seen


def _air_gapped(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    settings = Settings()
    monkeypatch.setattr(
        CliSettingsLoader, "load", lambda self: settings.model_copy(update={"air_gapped": value})
    )


def test_an_air_gapped_run_hands_the_decision_to_the_processes_it_starts(monkeypatch, session):
    monkeypatch.delenv(AIR_GAPPED_ENV, raising=False)
    monkeypatch.setenv(AgnoTelemetry.ENV, "true")
    seen = _watch_start(monkeypatch)
    _air_gapped(monkeypatch, True)
    result = _invoke()
    assert result.exit_code == 0, result.output
    assert seen[0][AIR_GAPPED_ENV] == "1"
    assert seen[0][AgnoTelemetry.ENV] != "true", "Agno's telemetry stays off in an air gap"


def test_a_run_that_is_not_air_gapped_leaves_both_alone(monkeypatch, session):
    monkeypatch.delenv(AIR_GAPPED_ENV, raising=False)
    monkeypatch.setenv(AgnoTelemetry.ENV, "true")
    seen = _watch_start(monkeypatch)
    _air_gapped(monkeypatch, False)
    _invoke()
    assert AIR_GAPPED_ENV not in seen[0]
    assert seen[0][AgnoTelemetry.ENV] == "true"


def test_a_subcommand_starts_no_observability(tmp_path, monkeypatch):
    seen = _watch_start(monkeypatch)
    _invoke("ext", "list", "--root", str(tmp_path / "s"))
    assert seen == []


# ── a worktree is cleaned up when only the pointer was shown ───────


def test_bare_igni_in_a_worktree_removes_the_worktree_it_made(tmp_path, monkeypatch, session):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for argv in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "one"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, env=env)
    monkeypatch.chdir(repo)
    # The suite points ``Path.cwd()`` at a scratch directory; this run is in the repository.
    monkeypatch.setattr("pathlib.Path.cwd", lambda: repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    created: list[Path] = []
    original = WorktreeManager.create_result

    def spy(self: WorktreeManager, *args: Any, **kwargs: Any):
        result = original(self, *args, **kwargs)
        if result.info is not None:
            created.append(Path(result.info.worktree_path))
        return result

    monkeypatch.setattr(WorktreeManager, "create_result", spy)
    monkeypatch.setattr(CliSettingsLoader, "load", lambda self: Settings())
    result = _invoke("--worktree")
    assert result.exit_code == 0, result.output
    assert "Worktree: " in result.output
    assert "no longer ships a terminal chat UI" in result.output
    assert len(created) == 1
    assert not created[0].exists(), "the pointer run left its worktree behind"


def test_the_module_describes_the_command_by_its_own_name():
    doc = cli_module.__doc__ or ""
    assert "``igni -m" in doc and "``igni -p``" in doc
    assert "ember" not in doc.lower()
