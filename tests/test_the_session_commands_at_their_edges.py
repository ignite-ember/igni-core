"""``/clear``, ``/sessions``, ``/rename`` and ``/fork`` against a session's state.

A small stand-in session holds what these commands change — the id, the
latched context counter, whether the session counts as named — so the
tests read what a person's session ends up as, not which calls were made.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from igni_core.backend.command_handler import CommandHandler
from igni_core.core.activity.background import background
from igni_core.core.session.compaction.context_budget import ContextBudget
from igni_core.core.session.schemas import ForkResult, PersistResult
from igni_core.protocol.messages import CommandAction, CommandResultKind


class Identity:
    def __init__(self, named: bool = False) -> None:
        self.named = named

    def mark_named(self) -> None:
        self.named = True

    def set_named(self, named: bool) -> None:
        self.named = named


class FakeSession:
    def __init__(self, *, hook=None, named: bool = False) -> None:
        self.session_id = "old00000"
        self.context_budget = ContextBudget(window=1000, last_input_tokens=777)
        self.identity = Identity(named)
        self.on_new_dialogue = hook
        self.persistence = SimpleNamespace(
            rename=AsyncMock(return_value=PersistResult(ok=True)),
            get_name=AsyncMock(return_value="stored"),
            fork=AsyncMock(),
        )

    def rotate_id(self, new_id: str) -> None:
        self.session_id = new_id


def _run(session: FakeSession, command: str):
    return CommandHandler(session).handle(command)  # type: ignore[arg-type]


# ── /clear ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_starts_a_fresh_eight_character_session_and_forgets_the_context_count():
    session = FakeSession()
    result = await _run(session, "/clear")
    await background.drain()
    assert result.action == CommandAction.CLEAR
    assert len(session.session_id) == 8 and session.session_id != "old00000"
    assert session.context_budget.last_input_tokens == 0


@pytest.mark.asyncio
async def test_each_clear_gives_a_different_session_id():
    session = FakeSession()
    await _run(session, "/clear")
    first = session.session_id
    await _run(session, "/clear")
    await background.drain()
    assert session.session_id != first


@pytest.mark.asyncio
async def test_clear_asks_the_server_whether_the_group_changed_when_a_hook_is_wired():
    calls: list[str] = []

    async def hook():
        calls.append("asked")
        return SimpleNamespace(error="")

    session = FakeSession(hook=hook)
    result = await _run(session, "/clear")
    await background.drain()
    assert result.action == CommandAction.CLEAR
    assert calls == ["asked"]


@pytest.mark.asyncio
async def test_a_failed_group_revalidation_is_a_warning_and_the_clear_still_stands(caplog):
    async def hook():
        return SimpleNamespace(error="server unreachable")

    session = FakeSession(hook=hook)
    with caplog.at_level(logging.WARNING, logger="igni_core.backend.cmd_session"):
        result = await _run(session, "/clear")
        await background.drain()
    assert result.action == CommandAction.CLEAR
    assert "group-policy revalidation failed: server unreachable" in caplog.text


@pytest.mark.asyncio
async def test_a_clear_without_a_hook_says_nothing_about_revalidation(caplog):
    session = FakeSession(hook=None)
    with caplog.at_level(logging.WARNING, logger="igni_core.backend.cmd_session"):
        await _run(session, "/clear")
        await background.drain()
    assert "revalidation" not in caplog.text


@pytest.mark.asyncio
async def test_sessions_opens_the_panel():
    result = await _run(FakeSession(), "/sessions")
    assert result.action == CommandAction.SESSIONS


# ── /rename ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_confirmed_rename_marks_the_session_named():
    session = FakeSession()
    result = await _run(session, "/rename  Quarterly plan ")
    assert result.kind == CommandResultKind.INFO
    assert result.content == "Session renamed to: Quarterly plan"
    session.persistence.rename.assert_awaited_once_with("Quarterly plan")
    assert session.identity.named is True


@pytest.mark.asyncio
async def test_a_unicode_name_is_stored_and_confirmed_as_given():
    session = FakeSession()
    result = await _run(session, "/rename Prüfung 計画")
    assert result.content == "Session renamed to: Prüfung 計画"


@pytest.mark.asyncio
async def test_a_failed_rename_says_why_and_does_not_mark_the_session_named():
    session = FakeSession()
    session.persistence.rename = AsyncMock(return_value=PersistResult(ok=False, error="disk full"))
    result = await _run(session, "/rename Plan")
    assert result.is_error()
    assert result.content == "Rename failed: disk full"
    assert session.identity.named is False


@pytest.mark.asyncio
async def test_a_rename_with_nothing_stored_is_refused_and_leaves_the_session_unnamed():
    session = FakeSession()
    session.persistence.get_name = AsyncMock(return_value="")
    result = await _run(session, "/rename Plan")
    assert result.is_error()
    assert "not saved until its first message" in result.content
    assert session.identity.named is False


@pytest.mark.asyncio
async def test_a_blank_name_is_a_usage_error_and_stores_nothing():
    session = FakeSession()
    for command in ("/rename", "/rename    "):
        result = await _run(session, command)
        assert result.is_error()
        assert result.content == "Usage: /rename <new session name>"
    session.persistence.rename.assert_not_awaited()


# ── /fork ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_named_fork_switches_to_the_new_id_and_counts_as_named():
    session = FakeSession()
    session.persistence.fork = AsyncMock(
        return_value=ForkResult(ok=True, new_session_id="fork0001")
    )
    result = await _run(session, "/fork  Try B ")
    session.persistence.fork.assert_awaited_once_with(name="Try B")
    assert result.action == CommandAction.FORK
    assert result.content == "fork0001"
    assert session.session_id == "fork0001"
    assert session.identity.named is True


@pytest.mark.asyncio
async def test_an_unnamed_fork_leaves_the_session_to_be_named_later():
    session = FakeSession(named=True)
    session.persistence.fork = AsyncMock(
        return_value=ForkResult(ok=True, new_session_id="fork0002")
    )
    await _run(session, "/fork")
    session.persistence.fork.assert_awaited_once_with(name=None)
    assert session.identity.named is False


@pytest.mark.asyncio
async def test_a_fork_with_a_blank_name_is_an_unnamed_fork():
    session = FakeSession()
    session.persistence.fork = AsyncMock(
        return_value=ForkResult(ok=True, new_session_id="fork0003")
    )
    await _run(session, "/fork    ")
    session.persistence.fork.assert_awaited_once_with(name=None)


@pytest.mark.asyncio
async def test_a_failed_fork_says_why_and_stays_on_the_source_session(caplog):
    session = FakeSession()
    session.persistence.fork = AsyncMock(
        return_value=ForkResult(ok=False, error="source session not found")
    )
    with caplog.at_level(logging.WARNING, logger="igni_core.backend.cmd_session"):
        result = await _run(session, "/fork")
    assert result.is_error()
    assert result.content == "Fork failed: source session not found"
    assert "/fork failed: source session not found" in caplog.text
    assert session.session_id == "old00000"


@pytest.mark.asyncio
async def test_a_fork_that_succeeds_without_an_id_stays_on_the_source_session():
    """Switching to an empty id would rebind every component to no session."""
    session = FakeSession()
    session.persistence.fork = AsyncMock(return_value=ForkResult(ok=True, new_session_id=""))
    result = await _run(session, "/fork")
    assert result.is_error()
    assert "no new session id" in result.content
    assert session.session_id == "old00000"


@pytest.mark.asyncio
async def test_a_failed_fork_with_no_reason_says_unknown_error():
    session = FakeSession()
    session.persistence.fork = AsyncMock(return_value=ForkResult(ok=False, error=None))
    result = await _run(session, "/fork")
    assert result.content == "Fork failed: unknown error"
