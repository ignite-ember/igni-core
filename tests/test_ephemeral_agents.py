"""Tests for ephemeral agent lifecycle — register, list, promote, limits."""

from unittest.mock import MagicMock, patch

import pytest

from igni_core.core.agents import AgentPool
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.tools.orchestrate import OrchestrateTools


class TestEphemeralInit:
    """AgentPool.init_ephemeral() sets up the temp directory."""

    def test_creates_agents_tmp_dir(self, tmp_path, settings):
        pool = AgentPool()
        pool.init_ephemeral(tmp_path)
        assert (tmp_path / CONFIG_DIR / "agents.tmp").is_dir()

    def test_idempotent(self, tmp_path, settings):
        pool = AgentPool()
        pool.init_ephemeral(tmp_path)
        pool.init_ephemeral(tmp_path)  # no error
        assert (tmp_path / CONFIG_DIR / "agents.tmp").is_dir()


class TestRegisterEphemeral:
    """AgentPool.ephemeral.register() creates and registers temp agents."""

    @pytest.fixture
    def pool(self, tmp_path, settings):
        p = AgentPool(settings=settings, project_dir=tmp_path)
        p.init_ephemeral(tmp_path)
        return p

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_register_creates_md_file(self, mock_build, pool, tmp_path):
        mock_build.return_value = MagicMock()
        pool.ephemeral.register(
            name="helper",
            description="A helper agent",
            system_prompt="You help with things.",
        )
        md_path = tmp_path / CONFIG_DIR / "agents.tmp" / "helper.md"
        assert md_path.exists()
        content = md_path.read_text()
        assert "name: helper" in content
        assert "description: A helper agent" in content
        assert "You help with things." in content

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_register_adds_to_definitions(self, mock_build, pool):
        mock_build.return_value = MagicMock()
        pool.ephemeral.register(
            name="helper",
            description="A helper",
            system_prompt="Help.",
        )
        assert pool.get_entry("helper") is not None

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_register_with_custom_tools(self, mock_build, pool, tmp_path):
        mock_build.return_value = MagicMock()
        pool.ephemeral.register(
            name="searcher",
            description="Searches code",
            system_prompt="Search.",
            # ``Read`` / ``Grep`` / ``Glob`` used to be the example
            # here. They were removed from the registry — searching and
            # reading go through ``Bash`` (``rg`` / ``cat``) — so the
            # fixture names tools that still resolve.
            tools=["Bash", "Edit", "WebSearch"],
        )
        md_path = tmp_path / CONFIG_DIR / "agents.tmp" / "searcher.md"
        content = md_path.read_text()
        assert "Bash, Edit, WebSearch" in content

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_register_with_model(self, mock_build, pool, tmp_path):
        mock_build.return_value = MagicMock()
        pool.ephemeral.register(
            name="smart",
            description="Smart agent",
            system_prompt="Be smart.",
            model="gpt-4o",
        )
        md_path = tmp_path / CONFIG_DIR / "agents.tmp" / "smart.md"
        content = md_path.read_text()
        assert "model: gpt-4o" in content

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_register_duplicate_raises(self, mock_build, pool):
        mock_build.return_value = MagicMock()
        pool.ephemeral.register(name="dup", description="d", system_prompt="p")
        with pytest.raises(ValueError, match="already exists"):
            pool.ephemeral.register(name="dup", description="d2", system_prompt="p2")

    def test_register_without_init_raises(self, settings):
        pool = AgentPool(settings=settings)
        with pytest.raises(RuntimeError, match="not initialized"):
            pool.ephemeral.register(name="x", description="d", system_prompt="p")

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_register_respects_limit(self, mock_build, pool):
        mock_build.return_value = MagicMock()
        pool.ephemeral.max_ephemeral = 2
        pool.ephemeral.register(name="a1", description="d", system_prompt="p")
        pool.ephemeral.register(name="a2", description="d", system_prompt="p")
        with pytest.raises(ValueError, match="limit reached"):
            pool.ephemeral.register(name="a3", description="d", system_prompt="p")


class TestListEphemeral:
    """EphemeralAgentStore.list_agents() returns only ephemeral agent definitions."""

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_list_ephemeral_agents(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.ephemeral.register(name="eph1", description="d", system_prompt="p")

        # Also add a non-ephemeral agent
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "perm.md").write_text(
            "---\nname: perm\ndescription: Permanent\n---\nPermanent agent.\n"
        )
        pool.load_directory(agents_dir, priority=0)

        ephemeral = pool.ephemeral.list_agents()
        assert len(ephemeral) == 1
        assert ephemeral[0].name == "eph1"

    def test_no_store_without_init(self):
        assert not AgentPool().has_ephemeral


class TestPromoteEphemeral:
    """AgentPool.ephemeral.promote() moves agent from tmp to permanent."""

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_promote_moves_file(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.ephemeral.register(name="promo", description="d", system_prompt="p")
        dest = pool.ephemeral.promote("promo", tmp_path)

        assert dest == tmp_path / CONFIG_DIR / "agents" / "promo.md"
        assert dest.exists()
        assert not (tmp_path / CONFIG_DIR / "agents.tmp" / "promo.md").exists()

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_promote_decrements_count(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.ephemeral.register(name="promo", description="d", system_prompt="p")
        assert pool.ephemeral.count == 1
        pool.ephemeral.promote("promo", tmp_path)
        assert pool.ephemeral.count == 0

    def test_promote_nonexistent_raises(self, tmp_path, settings):
        pool = AgentPool(settings=settings)
        pool.init_ephemeral(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            pool.ephemeral.promote("ghost", tmp_path)

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_promote_non_ephemeral_raises(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        # Add a permanent agent
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "perm.md").write_text(
            "---\nname: perm\ndescription: Permanent\n---\nPermanent.\n"
        )
        pool.load_directory(agents_dir, priority=0)

        with pytest.raises(ValueError, match="not an ephemeral"):
            pool.ephemeral.promote("perm", tmp_path)


class TestDiscardEphemeral:
    """AgentPool.ephemeral.discard() deletes an ephemeral agent."""

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_discard_removes_file_and_definition(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.ephemeral.register(name="temp", description="d", system_prompt="p")
        md_path = tmp_path / CONFIG_DIR / "agents.tmp" / "temp.md"
        assert md_path.exists()
        assert pool.get_entry("temp") is not None

        pool.ephemeral.discard("temp")
        assert not md_path.exists()
        assert pool.get_entry("temp") is None

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_discard_decrements_count(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.ephemeral.register(name="temp", description="d", system_prompt="p")
        assert pool.ephemeral.count == 1
        pool.ephemeral.discard("temp")
        assert pool.ephemeral.count == 0

    def test_discard_nonexistent_raises(self, tmp_path, settings):
        pool = AgentPool(settings=settings)
        pool.init_ephemeral(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            pool.ephemeral.discard("ghost")

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_discard_non_ephemeral_raises(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "perm.md").write_text(
            "---\nname: perm\ndescription: Permanent\n---\nPermanent.\n"
        )
        pool.load_directory(agents_dir, priority=0)

        with pytest.raises(ValueError, match="not an ephemeral"):
            pool.ephemeral.discard("perm")

    def test_discard_without_init_raises(self, settings):
        pool = AgentPool(settings=settings)
        with pytest.raises(RuntimeError, match="not initialized"):
            pool.ephemeral.discard("x")


class TestCleanupEphemeral:
    """EphemeralAgentStore.cleanup() removes all ephemeral agents."""

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_cleanup_removes_all(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.ephemeral.register(name="e1", description="d", system_prompt="p")
        pool.ephemeral.register(name="e2", description="d", system_prompt="p")
        assert pool.ephemeral.count == 2

        removed = pool.ephemeral.cleanup()
        assert removed == 2
        assert pool.ephemeral.count == 0
        assert not (tmp_path / CONFIG_DIR / "agents.tmp" / "e1.md").exists()
        assert not (tmp_path / CONFIG_DIR / "agents.tmp" / "e2.md").exists()
        assert pool.get_entry("e1") is None
        assert pool.get_entry("e2") is None

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_cleanup_leaves_permanent_agents(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.ephemeral.register(name="eph", description="d", system_prompt="p")

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "perm.md").write_text(
            "---\nname: perm\ndescription: Permanent\n---\nPermanent.\n"
        )
        pool.load_directory(agents_dir, priority=0)

        removed = pool.ephemeral.cleanup()
        assert removed == 1
        assert pool.get_entry("perm") is not None

    def test_cleanup_without_init_raises(self):
        with pytest.raises(RuntimeError, match="not initialized"):
            AgentPool().ephemeral.cleanup()


class TestCreateAgentTool:
    """OrchestrateTools.create_agent() creates ephemeral agents at runtime."""

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_create_agent_returns_confirmation(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path)

        tools = OrchestrateTools(
            pool=pool,
            settings=settings,
        )
        # Manually register create_agent (normally gated by settings)
        tools.register(tools.create_agent)

        result = tools.create_agent(
            name="debugger",
            description="Debugs issues",
            system_prompt="You debug code.",
        )
        assert "Created ephemeral agent" in result
        assert "debugger" in result
        assert pool.get_entry("debugger") is not None

    @patch("igni_core.core.agents.builder.AgentBuilder.build")
    def test_create_agent_error_on_limit(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool(settings=settings, project_dir=tmp_path)
        pool.init_ephemeral(tmp_path, max_ephemeral=0)

        tools = OrchestrateTools(pool=pool, settings=settings)

        result = tools.create_agent(
            name="x",
            description="d",
            system_prompt="p",
        )
        assert "Error" in result
