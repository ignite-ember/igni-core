"""``PanelsController`` — the flat ``backend.panels.<x>()`` calls over real panels.

The facade is what the RPC router calls. Here each delegator is read
against a real agent pool, skill pool and hooks map, and ``group_policy``
against a real policy cache on disk: the panel's content, not just its type.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from igni_core.backend.panels import (
    AgentsPanelController,
    HooksPanelController,
    OutputStylesCatalog,
    SkillsPanelController,
    SlashCommandsCatalog,
)
from igni_core.backend.schemas_rpc import GroupPolicyPackResult
from igni_core.backend.server_panels import PanelsController
from igni_core.core.agents import AgentPool
from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.meta import PackMeta
from igni_core.core.hooks.schemas import HookDefinition
from igni_core.core.skills import SkillPool


def _session(tmp_path: Path, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(storage=SimpleNamespace(data_dir=tmp_path / "data")),
        project_dir=tmp_path,
        **extra,
    )


def test_the_group_is_read_from_the_policy_cache(tmp_path) -> None:
    fetched = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    GroupPolicyCache.for_data_dir(tmp_path / "data").meta.save(
        PackMeta(
            group_id="g-1",
            group_name="Platform",
            fetched_at=fetched,
            entry_count=3,
            default_model="house-model",
        )
    )

    result = PanelsController(_session(tmp_path)).group_policy()  # type: ignore[arg-type]

    assert result == GroupPolicyPackResult(
        group_id="g-1",
        group_name="Platform",
        fetched_at=fetched.isoformat(),
        entry_count=3,
        default_model="house-model",
    )


def test_a_pack_never_fetched_has_no_time(tmp_path) -> None:
    GroupPolicyCache.for_data_dir(tmp_path / "data").meta.save(
        PackMeta(group_id="g-1", group_name="Platform", fetched_at=None)
    )

    result = PanelsController(_session(tmp_path)).group_policy()  # type: ignore[arg-type]

    assert (result.group_id, result.fetched_at, result.default_model) == ("g-1", None, None)


def test_without_group_metadata_the_answer_is_empty(tmp_path) -> None:
    assert PanelsController(_session(tmp_path)).group_policy() == GroupPolicyPackResult()  # type: ignore[arg-type]


def test_the_per_panel_controllers_are_reachable_and_stable(tmp_path) -> None:
    panels = PanelsController(_session(tmp_path))  # type: ignore[arg-type]

    assert isinstance(panels.agents, AgentsPanelController)
    assert isinstance(panels.hooks, HooksPanelController)
    assert isinstance(panels.skills, SkillsPanelController)
    assert isinstance(panels.slash_commands_catalog, SlashCommandsCatalog)
    assert isinstance(panels.output_styles_catalog, OutputStylesCatalog)
    assert panels.agents is panels.agents
    assert panels.skills is panels.skills


def test_skills_are_listed_from_the_session_pool(tmp_path) -> None:
    skills_dir = tmp_path / "skills"
    (skills_dir / "deploy").mkdir(parents=True)
    (skills_dir / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Ship it\nargument-hint: <env>\n---\nRun the deploy.\n"
    )
    pool = SkillPool()
    pool.load_directory(skills_dir)
    panels = PanelsController(_session(tmp_path, skill_pool=pool))  # type: ignore[arg-type]

    assert panels.skill_pool() is pool
    assert panels.skill_names() == ["deploy"]
    [info] = panels.skill_details()
    assert (info.name, info.description, info.argument_hint) == ("deploy", "Ship it", "<env>")
    assert "Run the deploy." in info.body


def test_hooks_are_listed_and_reloaded_through_the_session(tmp_path) -> None:
    hook = HookDefinition(type="command", command="echo hi", matcher="Bash", timeout=500)
    session = _session(
        tmp_path, hooks_map={"PreToolUse": [hook]}, reload_hooks=MagicMock(return_value=4)
    )
    panels = PanelsController(session)  # type: ignore[arg-type]

    [entry] = panels.hooks_details()
    assert (entry.event, entry.type, entry.command) == ("PreToolUse", "command", "echo hi")
    assert (entry.matcher, entry.timeout_ms) == ("Bash", 500)
    assert "4 active hook(s)" in panels.reload_hooks().text
    session.reload_hooks.assert_called_once_with()


def test_no_hooks_and_no_skills_are_empty_lists(tmp_path) -> None:
    panels = PanelsController(_session(tmp_path, hooks_map={}, skill_pool=SkillPool()))  # type: ignore[arg-type]

    assert panels.hooks_details() == []
    assert panels.skill_details() == []
    assert panels.skill_names() == []


@pytest.fixture
def agents_session(tmp_path, settings):
    repo = tmp_path / "repo"
    repo.mkdir()
    with patch("igni_core.core.agents.builder.AgentBuilder.build", return_value=MagicMock()):
        pool = AgentPool(settings=settings, project_dir=repo)
        pool.init_ephemeral(repo)
        yield SimpleNamespace(
            pool=pool,
            project_dir=repo,
            agent_tool_check=SimpleNamespace(by_agent=lambda: {}),
        )


def test_an_ephemeral_agent_is_listed_promoted_by_default_to_the_project_and_discarded(
    agents_session,
) -> None:
    panels = PanelsController(agents_session)  # type: ignore[arg-type]
    agents_session.pool.ephemeral.register(name="migrator", description="d", system_prompt="p")
    agents_session.pool.ephemeral.register(name="scratch", description="d", system_prompt="p")

    by_name = {a.name: a for a in panels.agent_details()}
    assert by_name["migrator"].is_ephemeral is True

    promoted = panels.promote_ephemeral_agent("migrator")
    assert promoted.ok and promoted.dest.endswith("agents/migrator.md")
    assert Path(promoted.dest).exists()

    assert panels.discard_ephemeral_agent("scratch").ok is True
    assert "scratch" not in {a.name for a in panels.agent_details()}

    bad = panels.promote_ephemeral_agent("nobody")
    assert bad.ok is False and bad.reason
    assert panels.discard_ephemeral_agent("nobody").ok is False
    agents_session.pool.ephemeral.register(name="other", description="d", system_prompt="p")
    assert panels.promote_ephemeral_agent("other", "nowhere").ok is False
