"""``igni --continue`` finds the newest session, and says so when it cannot look.

``ResumeResolver.latest_id`` reads the project's real session table. The
invocation test stubs the resolver; this one does not: real databases, real
sessions, and the three answers the CLI turns into its three messages.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from agno.session.agent import AgentSession

from igni_core.cli.resume import ResumeLookup, ResumeResolver
from igni_core.core.config.settings import Settings
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.memory.manager import StorageManager


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    result = Settings()
    result.storage.data_dir = tmp_path / "data"
    return result


async def _seed(settings: Settings, *sessions: tuple[str, int]) -> None:
    db = StorageManager.build_db(settings)
    for session_id, updated_at in sessions:
        await db.upsert_session(
            AgentSession(session_id=session_id, created_at=updated_at, updated_at=updated_at),
            deserialize=True,
        )


def test_a_fresh_install_has_nothing_to_resume(settings: Settings):
    assert ResumeResolver(settings).latest_id() == ResumeLookup()


@pytest.mark.asyncio
async def test_the_newest_of_several_sessions_is_the_one_resumed(settings: Settings):
    now = int(time.time())
    await _seed(settings, ("old", now - 100), ("newest", now), ("middle", now - 50))

    import asyncio

    lookup = await asyncio.to_thread(ResumeResolver(settings).latest_id)

    assert lookup == ResumeLookup(session_id="newest", error=None)


def test_a_database_that_cannot_be_built_is_an_error_not_a_crash(settings: Settings):
    settings.storage.database_url = "mysql://nobody@localhost/none"

    lookup = ResumeResolver(settings).latest_id()

    assert lookup.session_id is None
    assert lookup.error


def test_a_database_that_cannot_be_read_is_an_error_not_no_sessions(
    settings: Settings, tmp_path: Path
):
    """An unreachable server is not a fresh install: the CLI must say it could
    not look, not that there was nothing to resume."""
    settings.storage.database_url = DatabaseUrl(
        url="postgresql+psycopg://nobody:pw@127.0.0.1:1/none"
    ).url

    lookup = ResumeResolver(settings).latest_id()

    assert lookup.session_id is None
    assert lookup.error


@pytest.mark.asyncio
async def test_a_lookup_under_a_running_loop_is_an_error_not_a_resume(settings: Settings):
    lookup = ResumeResolver(settings).latest_id()

    assert lookup.session_id is None
    assert lookup.error is not None
