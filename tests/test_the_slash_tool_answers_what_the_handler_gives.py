"""``slash_command`` with a handler that answers a chosen result: what the
agent reads for an empty answer, a missing command, a bare slash, and a
blocked command behind leading whitespace."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from igni_core.backend import command_handler as cmd_mod
from igni_core.backend.command_result import CommandResult
from igni_core.core.tools.slash import SlashCommandTool


class _Handler:
    """Records what it was asked and answers ``answer``."""

    answer = CommandResult.info("")
    seen: list[str] = []

    def __init__(self, session):
        self.session = session

    async def handle(self, command):
        _Handler.seen.append(command)
        return _Handler.answer


@pytest.fixture
def tool(monkeypatch):
    _Handler.seen = []
    _Handler.answer = CommandResult.info("")
    monkeypatch.setattr(cmd_mod, "CommandHandler", _Handler)
    session = MagicMock()
    session.project_dir = Path("/tmp")
    return SlashCommandTool(session)


async def test_a_command_with_no_output_says_so(tool):
    assert await tool.slash_command("/compact") == "(no output)"


async def test_a_commands_content_is_returned_as_it_is(tool):
    _Handler.answer = CommandResult.markdown("# Title\n\nbody")

    assert await tool.slash_command("/ctx") == "# Title\n\nbody"


async def test_an_error_result_is_prefixed_so_it_reads_as_an_error(tool):
    _Handler.answer = CommandResult.error("no such thing")

    assert await tool.slash_command("/x") == "Error: no such thing"


async def test_no_command_at_all_is_an_empty_command_error(tool):
    assert await tool.slash_command(None) == "Error: empty command"  # type: ignore[arg-type]
    assert _Handler.seen == []


async def test_a_command_missing_its_slash_is_dispatched_with_one(tool):
    await tool.slash_command("compact now")

    assert _Handler.seen == ["/compact now"]


async def test_a_bare_slash_is_dispatched_as_it_is(tool):
    await tool.slash_command("/")

    assert _Handler.seen == ["/"]


@pytest.mark.parametrize("command", ["  /bypass", "\n/PLAN off", "\t/Accept"])
async def test_a_mode_command_behind_whitespace_is_still_refused_unrun(tool, command):
    result = await tool.slash_command(command)

    assert result.startswith("Error: /")
    assert "permission modes are the user's control" in result
    assert _Handler.seen == []


@pytest.mark.parametrize("command", ["  /quit", "\n/Model gpt", "/LOGOUT"])
async def test_a_user_only_command_behind_whitespace_is_still_refused_unrun(tool, command):
    result = await tool.slash_command(command)

    assert "would end the session, invalidate the current turn" in result
    assert _Handler.seen == []
