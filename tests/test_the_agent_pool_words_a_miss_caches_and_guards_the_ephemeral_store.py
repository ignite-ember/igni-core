"""What the agent pool tells and keeps, beyond what it loads.

A name nobody defines is an expected miss: ``get`` answers ``None`` and
``missing`` words it. A built agent is kept until something changes its
definition or the MCP set it was built against. The ephemeral store
exists only after ``init_ephemeral``.
"""

from __future__ import annotations

from itertools import count
from unittest.mock import MagicMock, patch

import pytest

from igni_core.core.agents import AgentBuilder, AgentDefinition, AgentPool, AgentPriority
from igni_core.core.agents.schemas import AgentEntry
from igni_core.core.config.settings import Settings
from igni_core.core.prompts import PromptFragments, PromptLoader
from igni_core.core.tools.process_supervisor import ProcessSupervisor


def _entry(name: str, priority: int = AgentPriority.PROJECT_LOCAL, description: str = "d"):
    return AgentEntry(
        definition=AgentDefinition(name=name, description=description),
        priority=AgentPriority(priority),
    )


def _write_agent(directory, name: str, body: str = "Be brief.") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {name} agent\n---\n{body}\n"
    )


@pytest.fixture
def built_agents():
    """Agent construction replaced by a counter: each build is a new object."""
    made = count()
    with (
        patch.object(AgentBuilder, "_agent_cls", side_effect=lambda **kw: (next(made), kw["name"])),
        patch.object(AgentBuilder, "_model_registry_cls"),
        patch.object(AgentBuilder, "_tool_registry_cls"),
    ):
        yield


class TestAMissIsWorded:
    def test_it_lists_the_names_sorted(self):
        pool = AgentPool()
        pool.upsert_entry(_entry("zeta"))
        pool.upsert_entry(_entry("alpha"))

        assert pool.missing("nope") == "Agent 'nope' not found. Available: alpha, zeta"

    def test_an_empty_pool_has_nothing_to_list(self):
        assert AgentPool().missing("nope") == "Agent 'nope' not found. Available: "

    def test_a_plugin_agent_name_is_quoted_whole(self):
        pool = AgentPool()
        pool.upsert_entry(_entry("plug:one"))

        assert pool.missing("plug:two") == "Agent 'plug:two' not found. Available: plug:one"


class TestEntriesAreExposed:
    def test_the_entries_and_definitions_follow_insertion_order(self):
        pool = AgentPool()
        pool.upsert_entry(_entry("b"))
        pool.upsert_entry(_entry("a"))

        assert [e.definition.name for e in pool.iter_entries()] == ["b", "a"]
        assert [d.name for d in pool.list_agents()] == ["b", "a"]

    def test_get_entry_answers_the_entry_or_none(self):
        pool = AgentPool()
        entry = _entry("a")
        pool.upsert_entry(entry)

        assert pool.get_entry("a") is entry
        assert pool.get_entry("b") is None

    def test_an_upsert_overwrites_whatever_the_priority(self):
        pool = AgentPool()
        pool.upsert_entry(_entry("a", AgentPriority.EPHEMERAL, "high"))
        pool.upsert_entry(_entry("a", AgentPriority.USER_CLAUDE, "low"))

        assert pool.get_entry("a").definition.description == "low"

    def test_remove_drops_the_entry_and_is_silent_when_absent(self):
        pool = AgentPool()
        pool.upsert_entry(_entry("a"))

        pool.remove("a")
        pool.remove("a")

        assert pool.get_entry("a") is None
        assert pool.get("a") is None


class TestAMergeNeverDemotes:
    def test_an_equal_priority_does_not_replace(self, tmp_path):
        pool = AgentPool(settings=Settings(), project_dir=tmp_path)
        first, second = tmp_path / "first", tmp_path / "second"
        _write_agent(first, "dup", "FIRST")
        _write_agent(second, "dup", "SECOND")

        pool.load_directory(first, AgentPriority.PROJECT_LOCAL)
        pool.load_directory(second, AgentPriority.PROJECT_LOCAL)

        assert "FIRST" in pool.get_entry("dup").definition.system_prompt

    def test_a_plain_int_priority_still_compares(self, tmp_path):
        pool = AgentPool(settings=Settings(), project_dir=tmp_path)
        low, high = tmp_path / "low", tmp_path / "high"
        _write_agent(low, "dup", "LOW")
        _write_agent(high, "dup", "HIGH")

        pool.load_directory(low, 1)
        pool.load_directory(high, 4)
        pool.load_directory(low, 2)

        assert "HIGH" in pool.get_entry("dup").definition.system_prompt


class TestAPluginDirectoryIsNamespacedAndStrict:
    def test_its_agents_carry_the_namespace_and_the_strict_restrictions(self, tmp_path):
        pool = AgentPool(settings=Settings(), project_dir=tmp_path)
        plugin = tmp_path / "agents"
        plugin.mkdir()
        (plugin / "helper.md").write_text(
            "---\nname: helper\ndescription: h\ntools: Bash, Read\n"
            "mcp_servers: [other]\n---\nHelp.\n"
        )

        report = pool.load_plugin_directory(plugin, AgentPriority.PROJECT_LOCAL, "plug")

        assert report.errors == []
        assert pool.get("nothing") is None
        defn = pool.get_entry("plug:helper").definition
        assert defn.name == "plug:helper"
        assert pool.get_entry("helper") is None
        # The strict envelope, not "no envelope": it is what load_directory
        # leaves when it is given no policy.
        bare = AgentPool(settings=Settings(), project_dir=tmp_path)
        bare.load_directory(plugin, AgentPriority.PROJECT_LOCAL, namespace="plug")
        loose = bare.get_entry("plug:helper").definition
        assert defn.force_isolation == "worktree"
        assert loose.force_isolation != "worktree"


class TestABuiltAgentIsKeptUntilItsDefinitionChanges:
    def test_get_builds_once_and_answers_the_same_object(self, built_agents, settings):
        pool = AgentPool(settings=settings)
        pool.upsert_entry(_entry("a"))

        assert pool.get("a") is pool.get("a")

    def test_an_upsert_makes_the_next_get_rebuild(self, built_agents, settings):
        pool = AgentPool(settings=settings)
        pool.upsert_entry(_entry("a"))
        first = pool.get("a")

        pool.upsert_entry(_entry("a", description="changed"))

        assert pool.get("a") is not first

    def test_an_upsert_of_another_name_keeps_the_built_one(self, built_agents, settings):
        pool = AgentPool(settings=settings)
        pool.upsert_entry(_entry("a"))
        first = pool.get("a")

        pool.upsert_entry(_entry("b"))

        assert pool.get("a") is first

    def test_remove_forgets_the_built_agent(self, built_agents, settings):
        pool = AgentPool(settings=settings)
        pool.upsert_entry(_entry("a"))
        pool.get("a")

        pool.remove("a")

        assert pool.get("a") is None

    def test_new_mcp_clients_make_the_next_get_rebuild(self, built_agents, settings):
        pool = AgentPool(settings=settings)
        pool.upsert_entry(_entry("a"))
        pool.build_agents({})
        first = pool.get("a")

        pool.build_agents({"srv": MagicMock()})

        assert pool.get("a") is not first

    def test_build_agents_needs_settings_first(self):
        with pytest.raises(AssertionError, match="load_definitions"):
            AgentPool().build_agents({})

    def test_a_pool_built_without_settings_builds_with_defaults(self, built_agents):
        pool = AgentPool()
        pool.upsert_entry(_entry("a"))

        assert pool.get("a") is not None


class TestTheEphemeralStoreExistsOnlyAfterItIsInitialised:
    def test_asking_before_init_says_so(self):
        pool = AgentPool()

        assert pool.has_ephemeral is False
        with pytest.raises(RuntimeError, match="init_ephemeral"):
            _ = pool.ephemeral

    def test_after_init_it_is_there(self, tmp_path):
        pool = AgentPool(settings=Settings(), project_dir=tmp_path)

        pool.init_ephemeral(tmp_path)

        assert pool.has_ephemeral is True
        assert pool.ephemeral is pool.ephemeral


class TestTheCollaboratorsTheyWereGiven:
    def test_the_db_is_the_one_handed_in(self):
        class Db:
            async def get_session(self, *a, **k): ...
            async def upsert_session(self, *a, **k): ...
            async def close(self): ...

        db = Db()

        assert AgentPool(db=db).db is db
        assert AgentPool().db is None

    def test_the_prompt_loader_is_the_one_handed_in_or_a_fresh_one(self):
        loader = PromptLoader(PromptFragments())

        assert AgentPool(prompt_loader=loader).prompt_loader is loader
        assert isinstance(AgentPool().prompt_loader, PromptLoader)

    def test_the_supervisor_reaches_the_build_context(self, built_agents, settings):
        supervisor = ProcessSupervisor()
        pool = AgentPool(settings=settings, supervisor=supervisor)
        pool.upsert_entry(_entry("a"))
        pool.get("a")

        assert pool._ensure_builder()._context.supervisor is supervisor

    def test_two_pools_without_a_supervisor_do_not_share_one(self):
        assert AgentPool()._supervisor is not AgentPool()._supervisor
