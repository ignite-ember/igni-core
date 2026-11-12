"""``/help`` — the panel, a topic, or the plain-text overview.

The panel action carries no content, so a surface with no panel (the
plain prompt, ``igni -m /help``) printed nothing at all. The overview is
what those surfaces print instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.builtin_command_registry import BUILTIN_REGISTRY
from igni_core.backend.cmd_help import cmd_help, help_overview
from igni_core.backend.command_handler import CommandHandler
from igni_core.backend.schemas_help import HelpTopicCatalog
from igni_core.protocol.messages import CommandAction, CommandResultKind

_HANDLER = SimpleNamespace()


class TestTheSlashCommand:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", ["", "   "])
    async def test_no_topic_opens_the_panel(self, args: str):
        result = await cmd_help(_HANDLER, args)
        assert result.action is CommandAction.HELP

    @pytest.mark.asyncio
    async def test_a_topic_renders_whatever_the_case_or_padding(self):
        catalog = HelpTopicCatalog.default()
        name = catalog.names()[0]
        expected = catalog.render(name)
        for typed in (name, f"  {name.upper()}  "):
            result = await cmd_help(_HANDLER, typed)
            assert result.content == expected.content
            assert result.kind is not CommandResultKind.ERROR

    @pytest.mark.asyncio
    async def test_an_unknown_topic_is_an_error_listing_the_known_ones(self):
        result = await cmd_help(_HANDLER, "No-Such-Topic")
        assert result.kind is CommandResultKind.ERROR
        assert "Unknown help topic: no-such-topic." in result.content
        for name in HelpTopicCatalog.default().names():
            assert name in result.content

    @pytest.mark.asyncio
    async def test_it_goes_through_the_handler(self):
        result = await CommandHandler(SimpleNamespace()).handle("/help")
        assert result.action is CommandAction.HELP


class TestTheOverview:
    def test_it_lists_every_command_with_its_description(self):
        text = help_overview()
        assert text.startswith("## Commands\n")
        for entry in BUILTIN_REGISTRY.entries():
            assert f"- `{entry.slash}` — {entry.description}" in text

    def test_it_lists_every_topic(self):
        text = help_overview()
        assert "## Topics" in text
        for name in HelpTopicCatalog.default().names():
            assert f"`/help {name}`" in text

    def test_it_is_not_empty(self):
        """The regression: the plain prompt printed nothing."""
        assert help_overview().strip()
