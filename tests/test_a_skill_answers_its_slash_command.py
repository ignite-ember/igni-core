"""The skill tier of the slash router, on a real skill pool.

A slash command no built-in, markdown or extension tier answers falls to
the skills: a user-invocable skill with that name runs, its body
rendered with the line's arguments and the session id; anything else is
the ordinary "Unknown command" naming the first word.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from igni_core.backend.skill_command_dispatcher import SkillCommandDispatcher
from igni_core.core.skills.loader import SkillPool
from igni_core.protocol.messages import CommandAction, CommandResultKind


def _dispatcher(
    tmp_path: Path, skills: dict[str, str] | None = None, session_id: str = "sess-1"
) -> SkillCommandDispatcher:
    """A dispatcher over a real pool loaded from ``<name>/SKILL.md`` files."""
    for name, text in (skills or {}).items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(text)
    pool = SkillPool()
    pool.load_directory(tmp_path)
    return SkillCommandDispatcher(SimpleNamespace(skill_pool=pool, session_id=session_id))


async def test_a_matching_skill_runs_its_body_with_the_arguments_and_the_session(tmp_path) -> None:
    dispatcher = _dispatcher(tmp_path, {"deploy": "ship $ARGUMENTS from ${IGNI_SESSION_ID}"})

    result = await dispatcher.match_and_render("/deploy  prod eu ")

    assert result.kind is CommandResultKind.INFO
    assert result.action is CommandAction.RUN_PROMPT
    assert result.content == "ship prod eu from sess-1"
    assert not result.is_unknown_command()


async def test_a_skill_without_arguments_renders_with_none(tmp_path) -> None:
    result = await _dispatcher(tmp_path, {"status": "[$ARGUMENTS]"}).match_and_render("/status")

    assert result.content == "[]"


async def test_an_unknown_command_names_only_its_first_word(tmp_path) -> None:
    result = await _dispatcher(tmp_path).match_and_render("/nothing at all")

    assert result.is_unknown_command()
    assert result.kind is CommandResultKind.ERROR
    assert result.content == "Unknown command: /nothing"


async def test_a_skill_the_user_may_not_invoke_is_unknown(tmp_path) -> None:
    hidden = "---\nname: internal\nuser-invocable: false\n---\nbody"
    result = await _dispatcher(tmp_path, {"internal": hidden}).match_and_render("/internal")

    assert result.is_unknown_command()


async def test_an_empty_line_is_unknown_not_a_crash(tmp_path) -> None:
    result = await _dispatcher(tmp_path).match_and_render("")

    assert result.is_unknown_command()
    assert result.kind is CommandResultKind.ERROR
