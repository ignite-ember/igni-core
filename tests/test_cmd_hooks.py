"""``/hooks`` — reload, list, or the panel.

Verbs are matched trimmed and case-insensitively; anything that is not a
verb opens the panel rather than failing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.cmd_hooks import cmd_hooks
from igni_core.core.hooks.schemas import HookDefinition
from igni_core.protocol.messages import CommandAction, CommandResultKind


class _Session:
    def __init__(self, hooks_map=None, reload_count: int = 0):
        self.hooks_map = hooks_map or {}
        self._count = reload_count
        self.reloads = 0

    def reload_hooks(self) -> int:
        self.reloads += 1
        return self._count


def _handler(session: _Session):
    return SimpleNamespace(session=session)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["reload", "RELOAD", "  Reload  "])
async def test_reload_reports_the_count_it_loaded(args: str):
    session = _Session(reload_count=3)
    result = await cmd_hooks(_handler(session), args)
    assert result.content == "Hooks reloaded. 3 hook(s) loaded."
    assert result.kind is CommandResultKind.INFO
    assert session.reloads == 1


@pytest.mark.asyncio
async def test_reloading_nothing_says_zero():
    result = await cmd_hooks(_handler(_Session(reload_count=0)), "reload")
    assert result.content == "Hooks reloaded. 0 hook(s) loaded."


@pytest.mark.asyncio
async def test_list_renders_each_hook_and_upper_case_works():
    session = _Session(
        {"PreToolUse": [HookDefinition(type="command", command="check.sh", matcher="Bash")]}
    )
    result = await cmd_hooks(_handler(session), "LIST")
    assert result.kind is CommandResultKind.MARKDOWN
    assert "- **PreToolUse**: `check.sh` (matcher: Bash)" in result.content
    assert session.reloads == 0


@pytest.mark.asyncio
async def test_list_with_no_hooks_says_so():
    result = await cmd_hooks(_handler(_Session()), "list")
    assert result.content == "No hooks loaded."


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "   ", "bogus", "reload now"])
async def test_anything_else_opens_the_panel(args: str):
    session = _Session(reload_count=5)
    result = await cmd_hooks(_handler(session), args)
    assert result.action is CommandAction.HOOKS
    assert not result.is_error()
    assert session.reloads == 0
