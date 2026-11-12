"""The action-only slash commands and ``/bug``.

``/quit``, ``/watcher``, ``/skills``, ``/mcp``, ``/extensions`` and
``/programs`` do nothing but ask the window to open something, so what
matters is that each asks for its own panel and ignores what follows it.
``/bug`` is the one that does work: it opens the issue tracker, and that
has to be the tracker of the repository this product's issues belong in.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend import cmd_misc
from igni_core.backend.cmd_misc import BugCommand, cmd_bug
from igni_core.backend.command_handler import CommandHandler
from igni_core.protocol.messages import CommandAction, CommandResultKind

_ACTIONS = [
    ("/quit", CommandAction.QUIT),
    ("/exit", CommandAction.QUIT),
    ("/watcher", CommandAction.WATCHER),
    ("/skills", CommandAction.SKILLS),
    ("/mcp", CommandAction.MCP),
    ("/extensions", CommandAction.EXTENSIONS),
    ("/programs", CommandAction.PROGRAMS),
]


class TestTheActionCommands:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("command", "action"), _ACTIONS)
    async def test_each_opens_its_own_panel(self, command: str, action: CommandAction):
        result = await CommandHandler(SimpleNamespace()).handle(command)
        assert result.action is action
        assert result.kind is CommandResultKind.ACTION

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("command", "action"), _ACTIONS)
    async def test_arguments_are_ignored(self, command: str, action: CommandAction):
        result = await CommandHandler(SimpleNamespace()).handle(f"{command} whatever you like")
        assert result.action is action

    @pytest.mark.asyncio
    async def test_extensions_and_programs_are_the_added_panels(self):
        """Pinned by name so the shims cannot be swapped for one another."""
        assert (await cmd_misc.cmd_extensions(None, "")).action is CommandAction.EXTENSIONS
        assert (await cmd_misc.cmd_programs(None, "")).action is CommandAction.PROGRAMS


class TestBug:
    @pytest.mark.asyncio
    async def test_it_opens_the_tracker_and_says_so(self, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(cmd_misc.BrowserOpener, "open", staticmethod(opened.append))

        result = await cmd_bug(None, "ignored")

        assert opened == [BugCommand.BUG_URL]
        assert result.kind is CommandResultKind.INFO
        assert result.content == f"Opened {BugCommand.BUG_URL}"

    @pytest.mark.asyncio
    async def test_a_browser_that_cannot_open_still_reports(self, monkeypatch):
        import webbrowser

        def boom(_url):
            raise webbrowser.Error("no browser")

        monkeypatch.setattr(webbrowser, "open", boom)
        result = await cmd_bug(None, "")
        assert result.content == f"Opened {BugCommand.BUG_URL}"

    def test_the_tracker_is_this_repository_s_own(self):
        """igni-core stands on its own: its bug command files issues
        against igni-core, not against a repository that lives elsewhere."""
        assert BugCommand.BUG_URL.endswith("/igni-core/issues")
        assert "igni-app" not in BugCommand.BUG_URL
