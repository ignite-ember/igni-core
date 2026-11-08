"""``InterruptedRunSummaryBuilder`` on real SQLite: stamped rows win.

The unstamped (hard-crash) path is pinned in ``test_crash_survival``;
this file pins the stamped path — the recap uses the stamped rows and
the recorded error, and Agno is not consulted — and that a failing
Agno session read degrades to the pending rows instead of blocking
startup.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from igni_core.backend.server_interrupted_run import InterruptedRunSummaryBuilder
from igni_core.core.session.pending_messages import PendingMessageStore


def _session() -> MagicMock:
    session = MagicMock()
    session.session_id = "sess"
    session.main_team.aget_session = AsyncMock(return_value=None)
    return session


@pytest.mark.asyncio
async def test_stamped_errored_row_drives_the_recap(tmp_path: Path) -> None:
    store = PendingMessageStore(tmp_path / "state.db")
    stamped = store.record_received("sess", "fix the build")
    store.record_received("sess", "unstamped sibling")
    store.mark_interrupted(stamped, "errored", last_error="rate limited")
    session = _session()

    summary = await InterruptedRunSummaryBuilder(session, store).build()

    assert summary is not None
    assert "The user had asked: 'fix the build'." in summary.recap
    assert "Reason: 'rate limited'." in summary.recap
    assert "unstamped sibling" not in summary.recap
    assert summary.pending_ids_to_drop == [stamped]
    session.main_team.aget_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_clean_shutdown_builds_nothing(tmp_path: Path) -> None:
    store = PendingMessageStore(tmp_path / "state.db")

    assert await InterruptedRunSummaryBuilder(_session(), store).build() is None


@pytest.mark.asyncio
async def test_a_locked_agno_db_still_yields_the_pending_row_summary(tmp_path: Path) -> None:
    store = PendingMessageStore(tmp_path / "state.db")
    pending = store.record_received("sess", "deploy it")
    session = _session()
    session.main_team.aget_session = AsyncMock(
        side_effect=OperationalError("SELECT", {}, Exception("database is locked")),
    )

    summary = await InterruptedRunSummaryBuilder(session, store).build()

    assert summary is not None
    assert "The user had asked: 'deploy it'." in summary.recap
    assert summary.pending_ids_to_drop == [pending]
    session.main_team.aget_session.assert_awaited_once()
