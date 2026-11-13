"""The plain-text REPL's slash-command dispatcher: what each action prints or runs.

The dispatcher turns a ``CommandResult`` into what a person at the
command line sees, or ends the REPL, or runs a prompt as a turn. The
session is a mock (it is a large object), the dispatcher and the real
``CommandHandler`` behind it are not.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.command_result import CommandResult
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.core.session.commands import LOGIN_HINT, InteractiveCommandDispatcher
from igni_core.core.session.mcp_ops import McpFailureCache
from igni_core.core.session.schemas import SessionListRow

from .test_commands import _make_session


def _printed(display: MagicMock, method: str) -> list[str]:
    return [call.args[0] for call in getattr(display, method).call_args_list]


@pytest.mark.asyncio
async def test_quit_ends_the_repl_with_a_clean_exit() -> None:
    session = _make_session()
    with pytest.raises(SystemExit) as ended:
        await InteractiveCommandDispatcher(session).dispatch("/quit")
    assert ended.value.code == 0


@pytest.mark.asyncio
async def test_a_bare_login_names_the_command_that_signs_in() -> None:
    session = _make_session()

    assert await InteractiveCommandDispatcher(session).dispatch("/login") is True

    assert _printed(session.display, "print_info") == [LOGIN_HINT]
    assert "`igni login`" in LOGIN_HINT
    # The TUI and its flag were removed; the hint must not send anyone to them.
    assert "--no-tui" not in LOGIN_HINT
    assert "TUI" not in LOGIN_HINT


@pytest.mark.asyncio
async def test_a_skill_command_is_run_as_a_turn_in_the_repl() -> None:
    session = _make_session()
    skill = MagicMock()
    skill.render.return_value = "RENDERED SKILL BODY"
    session.skill_pool.match_user_command.return_value = (skill, "args")
    ran: list[str] = []

    async def run_prompt(prompt: str) -> None:
        ran.append(prompt)

    handled = await InteractiveCommandDispatcher(session, run_prompt).dispatch("/myskill args")

    assert handled is True
    assert ran == ["RENDERED SKILL BODY"]
    # It was run, not shown.
    session.display.print_info.assert_not_called()


@pytest.mark.asyncio
async def test_a_skill_command_without_a_turn_pipeline_is_shown_not_lost() -> None:
    session = _make_session()
    skill = MagicMock()
    skill.render.return_value = "RENDERED SKILL BODY"
    session.skill_pool.match_user_command.return_value = (skill, "")

    assert await InteractiveCommandDispatcher(session).dispatch("/myskill") is True

    assert _printed(session.display, "print_info") == ["RENDERED SKILL BODY"]


@pytest.mark.asyncio
async def test_a_command_with_surrounding_whitespace_is_still_understood() -> None:
    session = _make_session()
    assert await InteractiveCommandDispatcher(session).dispatch("  /login \n") is True
    assert _printed(session.display, "print_info") == [LOGIN_HINT]


@pytest.mark.asyncio
async def test_sessions_are_capped_at_twenty_and_a_nameless_one_shows_its_id() -> None:
    session = _make_session()
    rows = [SessionListRow(session_id=f"id{i}", name=f"name{i}") for i in range(25)]
    rows[0] = SessionListRow(session_id="nameless-id")
    session.persistence.list_sessions = AsyncMock(return_value=rows)

    await InteractiveCommandDispatcher(session).dispatch("/sessions")

    (text,) = _printed(session.display, "print_markdown")
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == 20
    assert lines[0] == "- nameless-id"
    assert lines[-1] == "- name19"
    assert "name20" not in text
    assert "--resume" in text


@pytest.mark.asyncio
async def test_no_past_sessions_says_so() -> None:
    session = _make_session()
    session.persistence.list_sessions = AsyncMock(return_value=[])

    await InteractiveCommandDispatcher(session).dispatch("/sessions")

    assert _printed(session.display, "print_info") == ["No past sessions found."]


@pytest.mark.asyncio
async def test_the_model_list_is_refreshed_sorted_and_marks_the_current_one() -> None:
    session = _make_session()
    session.settings.models.registry = {"zeta": object(), "alpha": object(), "mid": object()}
    session.settings.models.default = "mid"

    await InteractiveCommandDispatcher(session).dispatch("/model")

    session.cloud_auth.refresh_models.assert_called_once_with()
    (text,) = _printed(session.display, "print_markdown")
    assert text.splitlines()[:4] == ["## Models", "- alpha", "- mid (current)", "- zeta"]


@pytest.mark.asyncio
async def test_a_model_added_on_the_portal_shows_up_in_the_list() -> None:
    """The refresh runs BEFORE the registry is read, so what it adds is listed."""
    session = _make_session()
    session.settings.models.registry = {"old": object()}

    def refresh() -> int:
        session.settings.models.registry["from-portal"] = object()
        return 1

    session.cloud_auth.refresh_models.side_effect = refresh

    await InteractiveCommandDispatcher(session).dispatch("/model")

    (text,) = _printed(session.display, "print_markdown")
    assert "- from-portal" in text


@pytest.mark.asyncio
async def test_mcp_lists_connected_servers_with_tool_counts_and_why_the_others_are_not() -> None:
    session = _make_session()
    mgr = session.mcp_manager
    mgr.list_servers.return_value = ["up", "broken", "quiet"]
    mgr.list_connected.return_value = ["up"]
    mgr.get_tools.return_value = ["t1", "t2", "t3"]
    failures = McpFailureCache()
    failures.record("broken", MCPConnectResult(ok=False, reason="spawn failed"))
    session.mcp_failures = failures

    await InteractiveCommandDispatcher(session).dispatch("/mcp")

    (text,) = _printed(session.display, "print_markdown")
    assert text.splitlines() == [
        "## MCP Servers",
        "- ● up — connected (3 tools)",
        "- ○ broken — error: spawn failed",
        "- ○ quiet — disconnected",
    ]
    mgr.get_tools.assert_called_once_with("up")


@pytest.mark.asyncio
async def test_clear_prints_the_new_session_id() -> None:
    session = _make_session()
    await InteractiveCommandDispatcher(session).dispatch("/clear")
    assert _printed(session.display, "print_info") == [
        "Conversation cleared. New session: abc12345"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "method"),
    [
        (CommandResult.markdown("md"), "print_markdown"),
        (CommandResult.info("inf"), "print_info"),
        (CommandResult.error("err"), "print_error"),
    ],
)
async def test_a_result_without_an_action_is_printed_by_its_kind(
    result: CommandResult, method: str
) -> None:
    session = _make_session()
    dispatcher = InteractiveCommandDispatcher(session)

    await dispatcher._render(result)

    assert _printed(session.display, method) == [result.content]
    for other in ("print_markdown", "print_info", "print_error"):
        if other != method:
            getattr(session.display, other).assert_not_called()
