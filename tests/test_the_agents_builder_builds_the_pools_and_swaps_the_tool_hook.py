"""The agents builder: the pools the orchestrator draws on, and the team's tool hook.

``build_pools`` is re-run on every plugin reload, so each run builds the
pools from scratch from the plugin set as it stands: a plugin the person
disabled contributes no agent and no skill. The session-wide cap on
sub-agents comes from ``orchestration.max_total_agents``, ephemeral
agents exist only when ``orchestration.generate_ephemeral`` is on, and the
agent-tool check is a diagnostic that never stops a start.

The main team's tool-event hook is rebuilt on a hook reload while the
team is not, so ``replace_tool_event_hook`` swaps that one hook in place
and leaves every other hook the team carries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.agents.tool_check import UnresolvedAgentTools
from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.hooks.tool_hook import ToolEventHook
from igni_core.core.session.core import Session
from igni_core.core.session.parts.agents import AgentsBuilder
from tests.session_factory import stub_project, stub_settings
from tests.session_plugin_rig import disable_plugin, write_plugin


def _session(root: Path, *, plugins: tuple[str, ...] = (), disabled: tuple[str, ...] = ()):
    project = stub_project(root / "proj")
    for name in plugins:
        write_plugin(project, name)
    settings = stub_settings(project, home=root / "home")
    if disabled:
        disable_plugin(Path(settings.storage.data_dir), *disabled)
    return Session(settings=settings, project_dir=project)


@pytest.fixture
def session(tmp_path: Path):
    return _session(tmp_path)


def test_an_enabled_plugin_contributes_its_agent_and_skill(tmp_path: Path) -> None:
    session = _session(tmp_path, plugins=("alpha",))

    assert "alpha:alpha-agent" in {a.name for a in session.pool.list_agents()}
    assert "alpha:alpha-skill" in {s.name for s in session.skill_pool.list_skills()}


def test_a_disabled_plugin_contributes_no_agent_and_no_skill(tmp_path: Path) -> None:
    session = _session(tmp_path, plugins=("alpha", "beta"), disabled=("beta",))

    agents = {a.name for a in session.pool.list_agents()}
    skills = {s.name for s in session.skill_pool.list_skills()}
    assert "alpha:alpha-agent" in agents
    assert "alpha:alpha-skill" in skills
    assert not any(name.startswith("beta:") for name in agents | skills)


def test_the_spawn_budget_is_the_configured_cap_and_a_rebuild_makes_a_fresh_one(session) -> None:
    assert session.spawn_budget.max_agents == session.settings.orchestration.max_total_agents

    session.settings.orchestration.max_total_agents = 3
    first = session.spawn_budget
    AgentsBuilder(session).build_pools()

    assert session.spawn_budget is not first
    assert session.spawn_budget.max_agents == 3


def test_ephemeral_agents_exist_only_when_generation_is_on(session) -> None:
    assert session.settings.orchestration.generate_ephemeral is True
    assert session.pool.has_ephemeral is True

    session.settings.orchestration.generate_ephemeral = False
    AgentsBuilder(session).build_pools()

    assert session.pool.has_ephemeral is False


def test_the_ephemeral_store_gets_the_configured_per_session_cap(session) -> None:
    session.settings.orchestration.max_ephemeral_per_session = 2

    AgentsBuilder(session).build_pools()

    assert session.pool.ephemeral.max_ephemeral == 2


def test_a_rebuild_drops_a_plugin_agent_whose_plugin_was_disabled(tmp_path: Path) -> None:
    session = _session(tmp_path, plugins=("alpha",))
    assert "alpha:alpha-agent" in {a.name for a in session.pool.list_agents()}

    session.disabled_plugins = {"alpha"}
    AgentsBuilder(session).build_pools()

    assert "alpha:alpha-agent" not in {a.name for a in session.pool.list_agents()}
    assert "alpha:alpha-skill" not in {s.name for s in session.skill_pool.list_skills()}


def test_the_agent_tool_check_is_stored_on_the_session(session) -> None:
    assert isinstance(session.agent_tool_check, UnresolvedAgentTools)


def test_a_failing_agent_tool_check_gives_an_empty_result_instead_of_stopping(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> list:
        raise RuntimeError("the pool cannot list")

    monkeypatch.setattr(session.pool, "list_agents", broken)

    checked = AgentsBuilder(session).check_tools()

    assert checked == UnresolvedAgentTools()


class _OtherHook:
    """A hook of some other kind the team carries."""


def test_the_team_is_built_with_igni_s_tool_event_hook_attached(session) -> None:
    hooks = session.main_team.tool_hooks

    assert sum(isinstance(h, ToolEventHook) for h in hooks) == 1


async def test_the_team_s_tool_hook_blocks_a_write_in_plan_mode(session) -> None:
    session.mode.set_permission_mode(PermissionMode.PLAN.value)
    hook = next(h for h in session.main_team.tool_hooks if isinstance(h, ToolEventHook))
    ran: list[str] = []

    def write(**_kwargs) -> str:
        ran.append("ran")
        return "written"

    result = await hook(name="Write", func=write, args={"file_path": "a.txt"})

    assert ran == []
    assert result != "written"


def test_replacing_the_hook_keeps_the_other_hooks_and_drops_every_old_one(session) -> None:
    other = _OtherHook()
    old_a = ToolEventHook(executor=session.hooks.executor)
    old_b = ToolEventHook(executor=session.hooks.executor)
    session.main_team.tool_hooks = [old_a, other, old_b]
    new = session.tool_hook_factory.create()

    AgentsBuilder(session).replace_tool_event_hook(new)

    assert session.main_team.tool_hooks == [other, new]


def test_replacing_the_hook_on_a_team_with_no_hook_list_installs_the_new_one(session) -> None:
    session.main_team.tool_hooks = None
    new = session.tool_hook_factory.create()

    AgentsBuilder(session).replace_tool_event_hook(new)

    assert session.main_team.tool_hooks == [new]


def test_replacing_the_hook_with_no_team_is_a_no_op(session) -> None:
    session.main_team = None

    AgentsBuilder(session).replace_tool_event_hook(session.tool_hook_factory.create())

    assert session.main_team is None
