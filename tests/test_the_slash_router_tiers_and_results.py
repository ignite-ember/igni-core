"""The slash router's tiers, and what a command result says about itself.

``CommandHandler.handle`` tries the built-ins, then markdown commands,
then what an installed extension contributes, then the skills. A result
answers whether it was a command at all (``is_unknown_command``), what
to show (``render_line``) and whether to dispatch on it (``is_action``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend import command_handler as handler_module
from igni_core.backend.command_handler import CommandHandler
from igni_core.backend.command_result import CommandResult, UnknownCommandResult
from igni_core.core.contributed_commands import ContributedCommands
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import CommandAction, CommandResultKind


def _session(tmp_path: Path) -> MagicMock:
    session = MagicMock()
    session.project_dir = tmp_path
    session.settings.rules.cross_tool_support = False
    session.group_policy.shipped_dir.return_value = None
    session.skill_pool.match_user_command.return_value = None
    session.session_id = "abc12345"
    return session


@pytest.fixture
def extensions(monkeypatch: pytest.MonkeyPatch) -> ContributedCommands:
    """A private table of extension commands, so none leaks between tests."""
    table = ContributedCommands()
    monkeypatch.setattr(handler_module, "contributed_commands", table)
    return table


def _nothing_discovered(*_args, **_kwargs) -> dict:
    return {}


def _handler(tmp_path: Path, discover=_nothing_discovered) -> CommandHandler:
    return CommandHandler(_session(tmp_path), discover=discover)


# ── routing ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_command_is_unknown_not_a_crash(tmp_path):
    for blank in ("", "   ", "\n\t"):
        result = await _handler(tmp_path).handle(blank)
        assert isinstance(result, UnknownCommandResult)
        assert result.is_unknown_command() is True
        assert result.is_error()


@pytest.mark.asyncio
async def test_the_command_name_is_matched_in_lower_case(tmp_path):
    result = await _handler(tmp_path).handle("/SESSIONS")
    assert result.action == CommandAction.SESSIONS


@pytest.mark.asyncio
async def test_a_name_that_lower_cases_longer_keeps_its_arguments_whole(tmp_path, extensions):
    """``İ`` lower-cases to two characters; the arguments must not lose
    the first characters of what follows the name."""
    seen: list[str] = []

    async def invoke(args: str) -> str:
        seen.append(args)
        return "ok"

    extensions.register("i̇x", owner="ext", description="d", invoke=invoke)
    result = await _handler(tmp_path).handle("/İx  hello world")
    assert result.content == "ok"
    assert seen == ["hello world"]


@pytest.mark.asyncio
async def test_an_extension_command_answers_with_its_text_as_markdown(tmp_path, extensions):
    async def invoke(args: str) -> str:
        return f"scanned {args}"

    extensions.register("compliance", owner="ext", description="d", invoke=invoke)
    result = await _handler(tmp_path).handle("/compliance  the repo ")
    assert result.kind == CommandResultKind.MARKDOWN
    assert result.content == "scanned the repo"


@pytest.mark.asyncio
async def test_an_extension_cannot_shadow_a_built_in(tmp_path, extensions):
    async def invoke(args: str) -> str:
        raise AssertionError("the built-in must answer")

    extensions.register("sessions", owner="ext", description="d", invoke=invoke)
    result = await _handler(tmp_path).handle("/sessions")
    assert result.action == CommandAction.SESSIONS


@pytest.mark.asyncio
async def test_a_markdown_command_cannot_shadow_a_built_in(tmp_path):
    def discover(*_a, **_k):
        raise AssertionError("the built-in must answer before discovery")

    result = await _handler(tmp_path, discover).handle("/sessions")
    assert result.action == CommandAction.SESSIONS


@pytest.mark.asyncio
async def test_a_markdown_command_wins_over_an_extension_and_a_skill(tmp_path, extensions):
    async def invoke(args: str) -> str:
        raise AssertionError("markdown goes first")

    extensions.register("deploy", owner="ext", description="d", invoke=invoke)
    command = MagicMock()
    command.render = AsyncMock(return_value="ship now")
    result = await _handler(tmp_path, lambda *_a, **_k: {"deploy": command}).handle("/deploy now")
    assert result.content == "ship now"


@pytest.mark.asyncio
async def test_an_extension_command_wins_over_a_skill(tmp_path, extensions):
    async def invoke(args: str) -> str:
        return "from the extension"

    extensions.register("review", owner="ext", description="d", invoke=invoke)
    handler = _handler(tmp_path)
    handler.session.skill_pool.match_user_command.side_effect = AssertionError("skills go last")
    result = await handler.handle("/review")
    assert result.content == "from the extension"


@pytest.mark.asyncio
async def test_a_failing_extension_command_is_an_error_and_never_reaches_the_skills(
    tmp_path, extensions
):
    async def invoke(args: str) -> str:
        raise RuntimeError("licence expired")

    extensions.register("compliance", owner="ext", description="d", invoke=invoke)
    handler = _handler(tmp_path)
    handler.session.skill_pool.match_user_command.side_effect = AssertionError("skills go last")
    result = await handler.handle("/compliance")
    assert result.is_error()
    assert not result.is_unknown_command()
    assert result.content == "licence expired"


@pytest.mark.asyncio
async def test_a_skill_answers_what_no_other_tier_owns(tmp_path):
    handler = _handler(tmp_path)
    skill = MagicMock()
    skill.render.return_value = "do the skill"
    handler.session.skill_pool.match_user_command.return_value = (skill, "args")
    result = await handler.handle("/myskill args")
    assert result.action == CommandAction.RUN_PROMPT
    assert result.content == "do the skill"


@pytest.mark.asyncio
async def test_a_command_nobody_owns_is_unknown_and_names_what_was_typed(tmp_path):
    result = await _handler(tmp_path).handle("/nonesuch with args")
    assert isinstance(result, UnknownCommandResult)
    assert result.content == "Unknown command: /nonesuch"


def test_the_catalog_lists_bare_built_in_names_and_describes_them():
    names = CommandHandler.builtin_names()
    assert "sessions" in names
    assert all(not n.startswith("/") for n in names)
    assert CommandHandler.describe("sessions")
    assert CommandHandler.describe("/sessions") == CommandHandler.describe("sessions")
    assert CommandHandler.describe("no-such-command") == ""


def test_the_handler_exposes_the_session_it_owns(tmp_path):
    session = _session(tmp_path)
    assert CommandHandler(session).session is session


# ── the result type ──────────────────────────────────────────────


def test_the_factories_carry_their_text_in_content():
    assert CommandResult.markdown("# m").kind == CommandResultKind.MARKDOWN
    assert CommandResult.info("i").kind == CommandResultKind.INFO
    error = CommandResult.error("e")
    assert error.kind == CommandResultKind.ERROR and error.content == "e"


def test_a_fork_carries_the_new_session_id_with_the_fork_action():
    result = CommandResult.fork("new12345")
    assert result.kind == CommandResultKind.ACTION
    assert result.action == CommandAction.FORK
    assert result.content == "new12345"


def test_an_action_result_can_carry_content():
    bare = CommandResult.for_action(CommandAction.SESSIONS)
    assert bare.content == ""
    with_content = CommandResult.for_action(CommandAction.RUN_PROMPT, "go")
    assert (with_content.kind, with_content.action, with_content.content) == (
        CommandResultKind.ACTION,
        CommandAction.RUN_PROMPT,
        "go",
    )


def test_only_an_error_kind_is_an_error():
    assert CommandResult.error("e").is_error()
    for other in (
        CommandResult.info("i"),
        CommandResult.markdown("m"),
        CommandResult.for_action(CommandAction.SESSIONS),
    ):
        assert not other.is_error()


def test_a_result_with_an_action_is_an_action_whatever_its_kind():
    info_with_action = CommandResult(
        kind=CommandResultKind.INFO, content="x", action=CommandAction.SCHEDULE
    )
    assert info_with_action.is_action()
    assert CommandResult.for_action(CommandAction.SESSIONS).is_action()
    assert not CommandResult.info("plain").is_action()
    assert not CommandResult.error("plain").is_action()


def test_the_chat_line_prefers_display_content_and_falls_back_when_it_is_empty():
    shown = CommandResult(kind=CommandResultKind.INFO, content="wrapped", display_content="shown")
    assert shown.render_line() == "shown"
    empty = CommandResult(kind=CommandResultKind.INFO, content="wrapped", display_content="")
    assert empty.render_line() == "wrapped"
    assert CommandResult.info("").render_line() == ""


def test_only_the_unknown_result_says_it_was_not_understood():
    unknown = UnknownCommandResult.unknown("zzz")
    assert unknown.is_unknown_command() is True
    assert unknown.is_error()
    assert unknown.content == "Unknown command: zzz"
    assert CommandResult.error("Unknown command: zzz").is_unknown_command() is False
    assert CommandResult.info("x").is_unknown_command() is False


def test_the_unknown_result_goes_over_the_wire_as_a_plain_error():
    unknown = UnknownCommandResult.unknown("zzz")
    plain = CommandResult.error("Unknown command: zzz")
    assert unknown.model_dump() == plain.model_dump()
    assert set(unknown.model_dump()) == set(msg.CommandResult.model_fields)
    assert unknown.model_dump()["type"] == "command_result"
