"""A trust change must build the team once, and against the new tooling.

The main team is built against the session's language-server and monitor
managers — the agent builder reads them off the session
(``agent_builder/coordinator.py``). A workspace-trust change stops the
old managers and builds new ones from the new configuration, so the
team has to be built *after* that: a team built against the old
managers holds managers that are no longer running.

It used to be built twice. The plugin-reload orchestrator rebuilt the
team at the end of its own reload, and the session rebuilt it again
once the tooling was in place — the first build was thrown away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.session.parts.agents import AgentsBuilder
from igni_core.core.session.parts.tooling import ToolingBuilder
from tests.session_factory import build_session


@pytest.fixture
def session(tmp_path: Path):
    return build_session(tmp_path)


@pytest.fixture
def builds(monkeypatch):
    """Every team and tooling build, in the order they happened."""
    events: list[str] = []

    original_team = AgentsBuilder.build_main_team
    original_tooling = ToolingBuilder.build

    def build_team(self) -> None:
        events.append("team")
        original_team(self)

    def build_tooling(self) -> None:
        events.append("tooling")
        original_tooling(self)

    monkeypatch.setattr(AgentsBuilder, "build_main_team", build_team)
    monkeypatch.setattr(ToolingBuilder, "build", build_tooling)
    return events


async def test_a_trust_change_builds_the_team_once(session, builds) -> None:
    await session.apply_workspace_trust()

    assert builds.count("team") == 1, (
        "the team was built more than once on a trust change — one of the builds was thrown away"
    )


async def test_the_team_is_built_after_the_new_tooling(session, builds) -> None:
    """The managers the team is built against must be the new ones."""
    await session.apply_workspace_trust()

    assert builds.index("tooling") < builds.index("team"), (
        "the team was built before the language-server and monitor managers "
        "were replaced, so it holds managers that are no longer running"
    )


def test_a_plugin_reload_builds_the_team_once(session, builds) -> None:
    """The plugin's custom tools reach the live agent, so the team is
    rebuilt — once."""
    session.reload_plugins()

    assert builds.count("team") == 1
