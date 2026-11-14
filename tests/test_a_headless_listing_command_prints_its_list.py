"""``igni -m /sessions`` (and ``/model``, ``/agents``, ``/skills``) prints a list.

These commands answer with an action and no content: in the clients the
action opens a picker or a panel. The headless run printed only content, so
each of them printed nothing and exited 0, as if there were nothing to list.
The run now prints the REPL's text form of each, and a command whose page
has no text form says so. Real :class:`SingleMessageRun` and command
handler; the session's stores and the display are stand-ins.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from igni_core.core.config.settings import Settings
from igni_core.core.session.schemas import SessionListRow
from igni_core.core.session.single_message_run import SingleMessageRun
from tests.session_factory import build_session


class _NoExtensions:
    def __init__(self, _session) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


@pytest.fixture
def session(tmp_path: Path):
    session = build_session(tmp_path)

    async def hook(*, event: str, payload) -> None:
        return None

    session.hook_executor.execute = hook
    session.display = MagicMock()
    session.handle_message = AsyncMock(return_value="the answer")
    return session


async def _run(session, message: str) -> str:
    with (
        patch("igni_core.core.session.session_run.Session", return_value=session),
        patch("igni_core.core.session.single_message_run.HeadlessExtensions", _NoExtensions),
    ):
        await SingleMessageRun(Settings()).run(message)
    session.handle_message.assert_not_called()
    display = session.display
    printed = [
        call.args[0]
        for method in (display.print_markdown, display.print_info)
        for call in method.call_args_list
    ]
    return "\n".join(printed)


async def test_sessions_lists_the_saved_sessions(session):
    session.persistence.list_sessions = AsyncMock(
        return_value=[SessionListRow(session_id="7997d259", name="greeting fix")]
    )

    text = await _run(session, "/sessions")

    assert "## Sessions" in text
    assert "greeting fix" in text


async def test_model_lists_the_registry_and_marks_the_current_one(session):
    session.cloud_auth.refresh_models = MagicMock(return_value=0)
    session.settings.models.registry = {"minimax": MagicMock(), "other": MagicMock()}
    session.settings.models.default = "minimax"

    text = await _run(session, "/model")

    assert "- minimax (current)" in text
    assert "- other" in text
    assert "`/model <name>`" in text
    assert "[dim]" not in text


async def test_agents_lists_each_agent_with_its_tools(session):
    agent = SimpleNamespace(name="coder", description="Writes code", tools=["edit_file", "grep"])
    session.pool.list_agents = MagicMock(return_value=[agent])

    text = await _run(session, "/agents")

    assert "- coder — Writes code" in text
    assert "  - tools: edit_file, grep" in text


async def test_skills_lists_each_skill_as_the_command_that_runs_it(session):
    skill = SimpleNamespace(name="review", description="Review a diff", user_invocable=True)
    session.skill_pool.list_skills = MagicMock(return_value=[skill])

    text = await _run(session, "/skills")

    assert "- /review — Review a diff" in text


async def test_a_command_whose_page_has_no_text_form_says_so(session):
    text = await _run(session, "/hooks")

    assert "`/hooks` opens a page in igni's app" in text


async def test_quit_prints_nothing(session):
    text = await _run(session, "/quit")

    assert text == ""
