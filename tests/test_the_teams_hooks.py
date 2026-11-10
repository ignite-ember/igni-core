"""The hooks on the main team, and what a reload does to them.

The main team is built with a tool-event hook attached, and the hook is
rebuilt whenever the hooks are reloaded — the team is not. Agno owns
the team's hook list and offers no setter, so the one hook that changed
is swapped in place: the old tool-event hook is dropped and the new one
appended.

The swap lives on the builder that owns the team
(``parts/agents.py``), because that is what knows how a tool-event hook
is attached to it. The session asks for the swap; it does not reach
into Agno's list itself.

The session's durability hooks are read off the session directly: the
memory mirror is declared on the session's surface and always set, so a
missing attribute must fail loudly rather than quietly drop the hook
that goes with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.hooks.tool_hook import ToolEventHook
from tests.session_factory import build_session


@pytest.fixture
def session(tmp_path: Path):
    return build_session(tmp_path)


def _tool_event_hooks(team) -> list[ToolEventHook]:
    return [h for h in (team.tool_hooks or []) if isinstance(h, ToolEventHook)]


def test_the_team_carries_one_tool_event_hook(session) -> None:
    assert len(_tool_event_hooks(session.main_team)) == 1


def test_reloading_the_hooks_replaces_the_tool_event_hook(session) -> None:
    """The hook is rebuilt, so the team's must be the new one."""
    before = _tool_event_hooks(session.main_team)
    assert before

    session.reload_hooks()

    after = _tool_event_hooks(session.main_team)
    assert len(after) == 1, "the old tool-event hook was left on the team"
    assert after[0] is not before[0], "the team still holds the hook from before the reload"


def test_the_other_hooks_stay_on_the_team(session) -> None:
    """Only the tool-event hook changed; the memory hooks are still there."""
    before = list(session.main_team.tool_hooks or [])

    session.reload_hooks()

    after = list(session.main_team.tool_hooks or [])
    assert len(after) == len(before), "a reload added a hook to the team"


def test_the_team_carries_the_turn_result_hook(session) -> None:
    """The session's durability hooks are read off the session directly.

    The memory mirror is declared on the session's surface and always
    set, so it is read directly: a missing attribute must fail loudly
    rather than quietly drop the hook that goes with it.
    """
    hooks = session.memory_sync_hooks()

    assert [type(h).__name__ for h in hooks] == ["TurnResultHook"]
