"""The plugin builder: hooks that read the MCP manager late, the factory, the reload.

The hooks are built before the MCP manager exists, so the resolver an
``mcp_tool`` hook uses reads the manager when it is asked and answers
"no such tool" while there is none. A plugin reload is the builders run
again, so a plugin added on disk shows up in the pools, the MCP servers
and the output styles without a restart, and the session's mirrors of the
hook executor and the hook map follow the hooks that were loaded.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.core.session.core import Session
from igni_core.core.session.parts.plugins import PluginBuilder
from tests.session_factory import build_session, stub_project, stub_settings
from tests.session_plugin_rig import write_plugin


@pytest.fixture
def session(tmp_path: Path):
    return build_session(tmp_path)


def test_the_hook_resolver_answers_no_tool_while_there_is_no_mcp_manager(session) -> None:
    del session.mcp_manager

    PluginBuilder(session).build_hooks()

    assert session.hooks.resolver.resolve("srv", "search") is None


def test_the_hook_resolver_reads_the_manager_the_session_has_when_asked(session) -> None:
    del session.mcp_manager
    PluginBuilder(session).build_hooks()
    asked: list[tuple[str, str]] = []

    def get_tool(server: str, name: str):
        asked.append((server, name))
        return lambda: "called"

    session.mcp_manager = SimpleNamespace(get_tool=get_tool)

    resolved = session.hooks.resolver.resolve("srv", "search")

    assert resolved is not None
    assert resolved() == "called"
    assert asked[0][0] == "srv"


def test_the_hooks_read_the_group_s_hooks_directory(session) -> None:
    PluginBuilder(session).build_hooks()

    assert session.hooks._group_hooks_dir == session.group_policy.dir_for("hooks")


def test_the_session_mirrors_the_executor_and_hook_map_of_the_loaded_hooks(session) -> None:
    session.hook_executor = None
    session.hooks_map = None

    PluginBuilder(session).mirror_hooks()

    assert session.hook_executor is session.hooks.executor
    assert session.hooks_map is session.hooks.hooks_map


def test_building_the_plugins_publishes_the_factory_and_its_evaluator(session) -> None:
    PluginBuilder(session).build_plugins_and_styles()

    assert session.permission_evaluator is session.tool_hook_factory.permission_evaluator
    assert session.tool_hook_factory.bypass_guard is not None
    assert session.tool_hook_factory.ask_person == session.approval.ask


def test_a_reload_picks_up_a_plugin_added_on_disk(tmp_path: Path) -> None:
    project = stub_project(tmp_path / "proj")
    settings = stub_settings(project, home=tmp_path / "home")
    session = Session(settings=settings, project_dir=project)
    assert "alpha:alpha-agent" not in {a.name for a in session.pool.list_agents()}
    write_plugin(project, "alpha")

    session.reload_plugins()

    assert "alpha:alpha-agent" in {a.name for a in session.pool.list_agents()}
    assert "alpha:alpha-skill" in {s.name for s in session.skill_pool.list_skills()}
    assert "alpha:srv" in session.mcp_manager.configs
    assert "alpha-style" in session.output_style.styles


def test_a_reload_picks_up_a_plugin_hook_into_the_sessions_hook_map(tmp_path: Path) -> None:
    project = stub_project(tmp_path / "proj")
    settings = stub_settings(project, home=tmp_path / "home")
    session = Session(settings=settings, project_dir=project)
    root = write_plugin(project, "alpha")
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}'
    )

    counts = session.reload_plugins()

    assert counts.hooks >= 1
    assert session.hooks_map is session.hooks.hooks_map
    assert session.hook_executor is session.hooks.executor
