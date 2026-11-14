"""The runtime builder: who the session is, and the services it runs on.

A fresh session gets a new 8-character id and is not named; a resumed one
keeps the id it was resumed with and is named. A session on a scoped
shared database is that database's scope, and any other session is the
person running the process. The cloud credentials are read from the
configured file, a credential change rebuilds the team, and the message
handler is built once yet acts on the team, the id and the budget the
session has now.
"""

from __future__ import annotations

import getpass
from pathlib import Path

import pytest
from agno.metrics import RunMetrics
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from igni_core.core.auth.credentials import CloudCredentials
from igni_core.core.session.core import Session
from igni_core.core.session.parts.runtime import RuntimeBuilder
from tests.session_factory import build_session, stub_project, stub_settings


@pytest.fixture
def session(tmp_path: Path):
    return build_session(tmp_path)


def test_a_fresh_session_has_a_new_eight_character_id_and_is_not_named(session) -> None:
    assert len(session.session_id) == 8
    assert session.session_named is False


def test_two_fresh_sessions_do_not_share_an_id(tmp_path: Path) -> None:
    one = build_session(tmp_path / "one")
    two = build_session(tmp_path / "two")

    assert one.session_id != two.session_id


def test_a_resumed_session_keeps_its_id_and_is_named(tmp_path: Path) -> None:
    project = stub_project(tmp_path / "proj")
    settings = stub_settings(project, home=tmp_path / "home")

    session = Session(settings=settings, project_dir=project, resume_session_id="resumed1")

    assert session.session_id == "resumed1"
    assert session.session_named is True


def test_an_empty_resume_id_is_a_fresh_session(tmp_path: Path) -> None:
    project = stub_project(tmp_path / "proj")
    settings = stub_settings(project, home=tmp_path / "home")

    session = Session(settings=settings, project_dir=project, resume_session_id="")

    assert len(session.session_id) == 8
    assert session.session_named is False


def test_a_desktop_session_is_the_person_running_the_process(session) -> None:
    assert session.user_id == getpass.getuser()


class _ScopedDb:
    def __init__(self, scope: str) -> None:
        self.scoped_user_id = scope


def test_a_session_on_a_scoped_database_is_that_scope(session) -> None:
    session.db = _ScopedDb("tenant-7")

    RuntimeBuilder(session).build_identity(None)

    assert session.user_id == "tenant-7"


def test_an_empty_scope_falls_back_to_the_person_running_the_process(session) -> None:
    session.db = _ScopedDb("")

    RuntimeBuilder(session).build_identity(None)

    assert session.user_id == getpass.getuser()


def test_the_identity_reads_the_team_and_persistence_the_session_has_later(session) -> None:
    session.rotate_id("rotated1")

    assert session.main_team.session_id == "rotated1"
    assert session.persistence.session_id == "rotated1"


async def test_a_question_with_nobody_to_answer_is_not_asked(session) -> None:
    assert await session.approval.ask("Bash", {}, "why", []) is None


async def test_the_approval_channel_puts_its_question_to_the_sessions_sub_agent_hitl(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def pushed(*_args, **_kwargs):
        raise RuntimeError("reached the session's coordinator")

    session.sub_agent_hitl.open_relay()
    monkeypatch.setattr(session.sub_agent_hitl, "push_requirement", pushed)

    with pytest.raises(RuntimeError, match="reached the session's coordinator"):
        await session.approval.ask("Bash", {}, "why", [])


def test_the_cloud_credentials_come_from_the_configured_file_and_server(tmp_path: Path) -> None:
    project = stub_project(tmp_path / "proj")
    settings = stub_settings(project, home=tmp_path / "home")
    settings.api_url = "https://igni.example.test"
    settings.auth.credentials_file = str(tmp_path / "creds.json")

    session = Session(settings=settings, project_dir=project)

    assert session.cloud_auth.server_url == "https://igni.example.test"
    assert session.cloud_auth.credentials._store.path == tmp_path / "creds.json"


def test_a_credential_change_rebuilds_the_main_team(session) -> None:
    before = session.main_team

    session.cloud_auth.replace(CloudCredentials(session.settings.auth.credentials_file))

    assert session.main_team is not before


class _Team:
    """A team that records what it was asked and returns a real run output."""

    def __init__(self, input_tokens: int) -> None:
        self.calls: list[str] = []
        self._input_tokens = input_tokens

    async def arun(self, message: str, **kwargs):
        self.calls.append(message)
        return RunOutput(
            status=RunStatus.completed,
            content="answer",
            metrics=RunMetrics(input_tokens=self._input_tokens),
        )


async def test_the_message_handler_runs_the_team_the_session_holds_now(session) -> None:
    replaced = _Team(1234)
    session.main_team = replaced

    assert await session.message_handler.handle("hello there") == "answer"

    assert len(replaced.calls) == 1
    assert "hello there" in replaced.calls[0]


async def test_the_message_handler_audits_under_the_id_the_session_has_now(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries: list = []
    monkeypatch.setattr(session.audit, "log", entries.append)
    session.main_team = _Team(10)
    session.rotate_id("rotated2")

    await session.message_handler.handle("hello")

    assert [e.session_id for e in entries] == ["rotated2"]


async def test_the_message_handler_measures_against_the_sessions_context_budget(session) -> None:
    session.main_team = _Team(4321)

    await session.message_handler.handle("hello")

    assert session.context_budget.last_input_tokens == 4321


async def test_the_message_handler_hands_the_queued_reminders_to_the_team_once(session) -> None:
    team = _Team(10)
    session.main_team = team
    session.reminders.queue("mind the tests")

    await session.message_handler.handle("first")
    await session.message_handler.handle("second")

    assert "<system-reminder>mind the tests</system-reminder>" in team.calls[0]
    assert "mind the tests" not in team.calls[1]
