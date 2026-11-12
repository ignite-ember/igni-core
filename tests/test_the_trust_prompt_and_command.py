"""Workspace trust on the command line: the first-run prompt, the notice, ``igni trust``.

``test_workspace_trust.py`` pins the service and the headless outcomes. This
pins what a person at a terminal sees and types: the three answers, the
refusal, when nobody can be asked, the notice's text, and each ``igni trust``
subcommand's output.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from igni_core import cli as cli_module
from igni_core.cli import trust as trust_module
from igni_core.cli.trust import SessionTrust
from igni_core.core.workspace_trust import (
    GATED_SUMMARY,
    WorkspaceIdentity,
    WorkspaceTrustStore,
    process_trust,
)
from igni_core.core.workspace_trust.policy import WorkspaceTrustConfig

pytestmark = pytest.mark.workspace_trust


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:acme/app.git\n'
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
    )
    return path


@pytest.fixture
def plain(tmp_path: Path) -> Path:
    path = tmp_path / "plain dir é"
    path.mkdir()
    return path


def _answer(text: str, folder: Path, **flags) -> tuple[bool, str]:
    """Run ``settle`` on a terminal where the person types ``text``; returns (result, stderr)."""
    settle = SessionTrust(
        folder,
        pipe=flags.get("pipe", False),
        trust_flag=flags.get("trust_flag", False),
        quiet=flags.get("quiet", False),
    ).settle
    with CliRunner().isolation(input=text) as streams:
        result = settle()
    return result, streams[2].getvalue().decode()


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trust_module, "_can_prompt", lambda: True)


# ── the prompt ────────────────────────────────────────────────────


class TestTheFirstRunPrompt:
    def test_t_trusts_and_remembers(self, terminal, repo: Path):
        result, shown = _answer("t\n", repo)

        assert result is True
        assert WorkspaceTrustStore().get(WorkspaceIdentity.of(repo)) == "trusted"
        assert "Trust this folder?" in shown
        assert f"Folder: {WorkspaceIdentity.of(repo).path}" in shown
        assert "Git remote: github.com/acme/app" in shown
        assert f"igni leaves off its {GATED_SUMMARY}." in shown

    def test_o_trusts_for_this_run_only(self, terminal, repo: Path):
        result, _ = _answer("o\n", repo)

        assert result is True
        assert process_trust().is_trusted(repo)
        assert WorkspaceTrustStore().get(WorkspaceIdentity.of(repo)) is None

    def test_d_records_a_distrust(self, terminal, repo: Path):
        result, _ = _answer("d\n", repo)

        assert result is False
        assert WorkspaceTrustStore().get(WorkspaceIdentity.of(repo)) == "untrusted"

    def test_enter_alone_is_dont_trust(self, terminal, repo: Path):
        result, _ = _answer("\n", repo)

        assert result is False
        assert WorkspaceTrustStore().get(WorkspaceIdentity.of(repo)) == "untrusted"

    def test_the_answer_is_not_case_sensitive(self, terminal, repo: Path):
        result, _ = _answer("T\n", repo)

        assert result is True
        assert WorkspaceTrustStore().get(WorkspaceIdentity.of(repo)) == "trusted"

    def test_an_invalid_answer_is_asked_again(self, terminal, repo: Path):
        result, shown = _answer("maybe\no\n", repo)

        assert result is True
        assert "Error:" in shown

    def test_a_folder_without_a_remote_says_none(self, terminal, plain: Path):
        _, shown = _answer("d\n", plain)

        assert "Git remote: none" in shown

    def test_a_policy_that_changed_underneath_says_not_trusted(self, terminal, repo: Path):
        """The folder was undecided when asked; policy now forbids trusting it."""
        service = process_trust()
        original = service.trust

        def refuse(*args, **kwargs):
            service.set_policy(WorkspaceTrustConfig(mode="never"))
            return original(*args, **kwargs)

        service.trust = refuse
        try:
            result, shown = _answer("t\n", repo)
        finally:
            service.trust = original

        assert result is False
        assert "  Not trusted: " in shown
        assert not service.is_trusted(repo)

    def test_an_already_trusted_folder_is_not_asked(self, terminal, repo: Path):
        process_trust().trust(repo)

        result, shown = _answer("", repo)

        assert result is False
        assert shown == ""

    def test_pipe_mode_never_asks_even_on_a_terminal(self, terminal, repo: Path):
        result, shown = _answer("t\n", repo, pipe=True)

        assert result is False
        assert "Trust this folder?" not in shown
        assert "is not trusted" in shown
        assert not process_trust().is_trusted(repo)

    def test_a_policy_that_already_decided_is_not_asked(self, terminal, repo: Path):
        process_trust().set_policy(WorkspaceTrustConfig(mode="never"))

        result, shown = _answer("t\n", repo)

        assert result is False
        assert "Trust this folder?" not in shown


# ── nobody to ask ─────────────────────────────────────────────────


class _Stream:
    def __init__(self, tty: bool | Exception) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        if isinstance(self._tty, Exception):
            raise self._tty
        return self._tty


class TestWhetherAnybodyCanBeAsked:
    @pytest.mark.parametrize(
        "stdin,stderr,expected",
        [
            (_Stream(True), _Stream(True), True),
            (_Stream(False), _Stream(True), False),
            (_Stream(True), _Stream(False), False),
            (None, _Stream(True), False),
            (_Stream(True), None, False),
            (_Stream(ValueError("closed")), _Stream(True), False),
            (_Stream(OSError("bad fd")), _Stream(True), False),
            (object(), _Stream(True), False),
        ],
    )
    def test_both_streams_must_be_terminals(self, monkeypatch, stdin, stderr, expected):
        monkeypatch.setattr("sys.stdin", stdin)
        monkeypatch.setattr("sys.stderr", stderr)

        assert trust_module._can_prompt() is expected


class TestTheNotice:
    @pytest.fixture(autouse=True)
    def _nobody(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(trust_module, "_can_prompt", lambda: False)

    def test_it_names_the_folder_the_gated_features_the_reason_and_the_way_out(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ):
        assert SessionTrust(repo, pipe=False, trust_flag=False, quiet=False).settle() is False

        path = WorkspaceIdentity.of(repo).path
        assert capsys.readouterr().err == (
            f"igni: {path} is not trusted — its {GATED_SUMMARY} are off. "
            f"{process_trust().decide(repo).reason} "
            "Run `igni trust` to trust it, or pass --trust for this run.\n"
        )

    def test_it_offers_no_way_out_when_policy_forbids_trusting(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ):
        process_trust().set_policy(WorkspaceTrustConfig(mode="never"))

        SessionTrust(repo, pipe=True, trust_flag=False, quiet=False).settle()

        shown = capsys.readouterr().err
        assert "is not trusted" in shown
        assert "igni trust" not in shown
        assert "--trust" not in shown

    def test_a_refused_trust_flag_is_told_the_same_way(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ):
        process_trust().set_policy(WorkspaceTrustConfig(mode="never"))

        assert SessionTrust(repo, pipe=True, trust_flag=True, quiet=False).settle() is False

        assert "is not trusted" in capsys.readouterr().err

    def test_quiet_says_nothing_either_way(self, repo: Path, capsys: pytest.CaptureFixture[str]):
        SessionTrust(repo, pipe=True, trust_flag=False, quiet=True).settle()
        process_trust().set_policy(WorkspaceTrustConfig(mode="never"))
        SessionTrust(repo, pipe=True, trust_flag=True, quiet=True).settle()

        assert capsys.readouterr().err == ""

    def test_an_already_trusted_folder_is_silent_and_not_re_granted(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ):
        process_trust().trust(repo)

        assert SessionTrust(repo, pipe=True, trust_flag=True, quiet=False).settle() is False

        assert capsys.readouterr().err == ""


# ── igni trust ────────────────────────────────────────────────────


def _igni(*args: str):
    return CliRunner().invoke(cli_module.cli, ["trust", *args])


class TestTheTrustCommand:
    def test_trusting_says_what_was_trusted(self, repo: Path):
        result = _igni(str(repo))

        path = WorkspaceIdentity.of(repo).path
        assert result.exit_code == 0, result.output
        assert (
            result.output
            == f"Trusted {path}\n  Folder: {path}\n  Git remote: github.com/acme/app\n"
        )

    def test_the_current_folder_is_the_default(self, repo: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("pathlib.Path.cwd", lambda: repo)

        result = _igni()

        assert result.exit_code == 0, result.output
        assert process_trust().is_trusted(repo)

    def test_a_folder_without_a_remote_says_none(self, plain: Path):
        result = _igni(str(plain))

        assert "  Git remote: none\n" in result.output
        assert str(plain.name) in result.output

    def test_a_trust_the_organisation_forbids_is_a_message_and_exit_one(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(WorkspaceTrustConfig.MODE_ENV, "never")

        result = _igni(str(repo))

        assert result.exit_code == 1
        assert result.output.startswith("Error: ")
        assert not process_trust().is_trusted(repo)

    def test_a_folder_that_is_not_there_is_refused(self, tmp_path: Path):
        result = _igni(str(tmp_path / "nowhere"))

        assert result.exit_code == 2
        assert "does not exist" in result.output

    def test_a_file_is_refused(self, tmp_path: Path):
        a_file = tmp_path / "file.txt"
        a_file.write_text("x")

        result = _igni(str(a_file))

        assert result.exit_code == 2
        assert "is a file" in result.output

    def test_revoking_says_igni_will_ask_again(self, repo: Path):
        _igni(str(repo))

        result = _igni("--revoke", str(repo))

        assert result.exit_code == 0, result.output
        assert (
            result.output
            == f"Trust revoked for {WorkspaceIdentity.of(repo).path}; igni will ask again.\n"
        )
        assert WorkspaceTrustStore().get(WorkspaceIdentity.of(repo)) is None

    def test_revoking_a_folder_never_decided_about_is_not_an_error(self, repo: Path):
        result = _igni("--revoke", str(repo))

        assert result.exit_code == 0, result.output
        assert "igni will ask again" in result.output

    def test_an_empty_list_says_so(self):
        result = _igni("--list")

        assert result.exit_code == 0, result.output
        assert result.output == "No folders trusted or distrusted yet.\n"

    def test_the_list_has_one_line_per_decision_with_state_path_and_remote(
        self, repo: Path, plain: Path
    ):
        process_trust().trust(repo)
        process_trust().distrust(plain)

        result = _igni("--list")

        assert result.exit_code == 0, result.output
        assert sorted(result.output.splitlines()) == sorted(
            [
                f"trusted    {WorkspaceIdentity.of(repo).path}  [github.com/acme/app]",
                f"untrusted  {WorkspaceIdentity.of(plain).path}  [no remote]",
            ]
        )

    def test_the_organisations_policy_is_read_before_it_decides(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Settings load first, so the managed policy applies to ``igni trust``."""
        assert os.environ.get(WorkspaceTrustConfig.MODE_ENV) is None
        monkeypatch.setenv(WorkspaceTrustConfig.MODE_ENV, "never")

        assert _igni(str(repo)).exit_code == 1
