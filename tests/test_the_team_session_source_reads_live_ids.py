"""The team session source looks the team and the ids up on every access."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from igni_core.core.session.compaction import TeamSessionSource


def _team(stored):
    return SimpleNamespace(aget_session=AsyncMock(return_value=stored))


def test_the_team_is_looked_up_each_time_so_a_rebuild_is_seen() -> None:
    state = {"team": _team(None)}
    source = TeamSessionSource(lambda: state["team"], lambda: "s", lambda: "u")
    first = source.team

    state["team"] = _team(None)

    assert source.team is state["team"]
    assert source.team is not first


def test_the_session_id_is_read_each_time_so_a_clear_is_seen() -> None:
    state = {"sid": "old"}
    source = TeamSessionSource(lambda: None, lambda: state["sid"], lambda: "u")
    assert source.session_id == "old"

    state["sid"] = "new"

    assert source.session_id == "new"


@pytest.mark.asyncio
async def test_load_asks_the_current_team_for_the_current_session_and_user() -> None:
    stored = object()
    state = {"team": _team(stored), "sid": "a"}
    source = TeamSessionSource(lambda: state["team"], lambda: state["sid"], lambda: "user-9")

    assert await source.load() is stored
    state["team"].aget_session.assert_awaited_once_with(session_id="a", user_id="user-9")

    state["sid"] = "b"
    await source.load()
    assert state["team"].aget_session.await_args.kwargs == {"session_id": "b", "user_id": "user-9"}


@pytest.mark.asyncio
async def test_load_is_none_for_a_missing_session_and_raises_what_the_team_raises() -> None:
    source = TeamSessionSource(lambda: _team(None), lambda: "s", lambda: "u")
    assert await source.load() is None

    broken = SimpleNamespace(aget_session=AsyncMock(side_effect=RuntimeError("db down")))
    source = TeamSessionSource(lambda: broken, lambda: "s", lambda: "u")
    with pytest.raises(RuntimeError, match="db down"):
        await source.load()
