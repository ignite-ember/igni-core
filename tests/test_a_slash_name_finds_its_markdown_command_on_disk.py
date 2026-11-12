"""A slash name is looked up among the markdown commands on disk, every time.

``tests/test_handle_markdown_command.py`` pins the branches with a stub
discovery. This drives :class:`MarkdownCommandDispatcher` with the real
discovery and real command files in a temporary project: what a person
who types ``/review`` sees when the file is there, when it is edited
between two calls, when it lives only under ``.claude/commands`` and
cross-tool support is off, or ships with the group.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.markdown_command_dispatcher import MarkdownCommandDispatcher
from igni_core.core.utils.markdown_commands import MarkdownCommand
from igni_core.protocol.messages import CommandAction, CommandResultKind


@pytest.fixture(autouse=True)
def _empty_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def _write(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(body)


def _dispatcher(
    project: Path, *, cross_tool: bool = True, group_dir: Path | None = None, discover: Any = None
) -> MarkdownCommandDispatcher:
    session = SimpleNamespace(
        project_dir=project,
        settings=SimpleNamespace(rules=SimpleNamespace(cross_tool_support=cross_tool)),
        group_policy=SimpleNamespace(
            shipped_dir=lambda kind: group_dir if kind == "commands" else None
        ),
    )
    return MarkdownCommandDispatcher(session, discover or MarkdownCommand.discover)  # type: ignore[arg-type]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


async def test_a_command_file_is_rendered_into_a_prompt_to_run(project):
    _write(project / ".igni" / "commands", "review", "Review $ARGUMENTS carefully.")

    result = await _dispatcher(project).try_render("/review", "the diff")

    assert result is not None
    assert (result.kind, result.action) == (CommandResultKind.INFO, CommandAction.RUN_PROMPT)
    assert result.content == "Review the diff carefully."


async def test_extra_leading_slashes_are_ignored_and_a_bare_slash_is_not_a_command(project):
    _write(project / ".igni" / "commands", "review", "body")
    calls: list[Any] = []

    def discover(*a: Any, **k: Any) -> dict:
        calls.append((a, k))
        return MarkdownCommand.discover(*a, **k)

    dispatcher = _dispatcher(project, discover=discover)

    assert (await dispatcher.try_render("//review", "")).content == "body"  # type: ignore[union-attr]
    calls.clear()
    assert await dispatcher.try_render("/", "x") is None
    assert await dispatcher.try_render("", "x") is None
    assert calls == [], "an empty name is not looked up"


async def test_a_name_with_no_file_falls_through_to_the_next_tier(project):
    _write(project / ".igni" / "commands", "review", "body")

    assert await _dispatcher(project).try_render("/deploy", "") is None


async def test_a_file_edited_between_two_calls_is_seen_by_the_second(project):
    commands = project / ".igni" / "commands"
    _write(commands, "review", "first draft")
    dispatcher = _dispatcher(project)
    assert (await dispatcher.try_render("/review", "")).content == "first draft"  # type: ignore[union-attr]

    _write(commands, "review", "second draft")
    assert (await dispatcher.try_render("/review", "")).content == "second draft"  # type: ignore[union-attr]

    (commands / "review.md").unlink()
    assert await dispatcher.try_render("/review", "") is None


async def test_claude_commands_are_read_only_with_cross_tool_support(project):
    _write(project / ".claude" / "commands", "triage", "from claude")

    on = await _dispatcher(project, cross_tool=True).try_render("/triage", "")
    off = await _dispatcher(project, cross_tool=False).try_render("/triage", "")

    assert on is not None and on.content == "from claude"
    assert off is None


async def test_a_command_the_group_ships_runs_and_the_project_s_own_beats_it(project, tmp_path):
    shipped = tmp_path / "group" / "commands"
    _write(shipped, "policy", "group policy")
    _write(shipped, "release", "group release")
    _write(project / ".igni" / "commands", "release", "project release")
    dispatcher = _dispatcher(project, group_dir=shipped)

    assert (await dispatcher.try_render("/policy", "")).content == "group policy"  # type: ignore[union-attr]
    assert (await dispatcher.try_render("/release", "")).content == "project release"  # type: ignore[union-attr]


async def test_a_broken_discovery_falls_through_without_an_error(project):
    def broken(*a: Any, **k: Any) -> dict:
        raise PermissionError("unreadable")

    assert await _dispatcher(project, discover=broken).try_render("/review", "") is None
