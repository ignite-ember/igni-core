"""``toolAudience: own-agents`` — a restricted MCP server reaches only the
agents whose definition file lies under the extension's own directory.

An agent that could grant itself the capability is the same as having no
rule, so the check is by resolved path and a doubt reads as a refusal.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from igni_core.core.agents import AgentBuilder, AgentDefinition
from igni_core.core.agents.schemas import AgentBuildContext
from igni_core.core.agents.tool_audience import ToolAudience, audience
from igni_core.core.tools.process_supervisor import ProcessSupervisor


class TestTheTable:
    def test_an_unrestricted_server_is_open_to_everyone(self, tmp_path):
        table = ToolAudience()

        assert table.allows("srv", None) is True
        assert table.allows("srv", tmp_path / "a.md") is True

    def test_an_agent_inside_the_directory_is_allowed(self, tmp_path):
        table = ToolAudience()
        table.restrict("srv", tmp_path / "plug")

        assert table.allows("srv", tmp_path / "plug" / "agents" / "a.md") is True

    def test_an_agent_elsewhere_is_refused(self, tmp_path):
        table = ToolAudience()
        table.restrict("srv", tmp_path / "plug")

        assert table.allows("srv", tmp_path / "other" / "a.md") is False

    def test_an_agent_with_no_file_is_refused(self, tmp_path):
        table = ToolAudience()
        table.restrict("srv", tmp_path / "plug")

        assert table.allows("srv", None) is False

    def test_a_sibling_sharing_a_name_prefix_is_not_inside(self, tmp_path):
        table = ToolAudience()
        table.restrict("srv", tmp_path / "ab")

        assert table.allows("srv", tmp_path / "abc" / "a.md") is False

    def test_dot_dot_cannot_climb_back_out(self, tmp_path):
        table = ToolAudience()
        table.restrict("srv", tmp_path / "plug")

        assert table.allows("srv", tmp_path / "plug" / ".." / "other" / "a.md") is False

    def test_a_symlink_out_of_the_directory_is_not_inside(self, tmp_path):
        (tmp_path / "plug").mkdir()
        (tmp_path / "other").mkdir()
        (tmp_path / "other" / "a.md").write_text("x")
        (tmp_path / "plug" / "link.md").symlink_to(tmp_path / "other" / "a.md")
        table = ToolAudience()
        table.restrict("srv", tmp_path / "plug")

        assert table.allows("srv", tmp_path / "plug" / "link.md") is False

    def test_a_symlinked_directory_is_judged_by_where_it_points(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "alias").symlink_to(real)
        table = ToolAudience()
        table.restrict("srv", tmp_path / "alias")

        assert table.allows("srv", real / "a.md") is True

    def test_a_relative_directory_is_resolved_against_the_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        table = ToolAudience()
        table.restrict("srv", Path("plug"))

        assert table.allows("srv", tmp_path / "plug" / "a.md") is True
        assert table.allows("srv", tmp_path / "x" / "a.md") is False

    def test_restricting_again_replaces_the_directory(self, tmp_path):
        table = ToolAudience()
        table.restrict("srv", tmp_path / "one")
        table.restrict("srv", tmp_path / "two")

        assert table.allows("srv", tmp_path / "one" / "a.md") is False
        assert table.allows("srv", tmp_path / "two" / "a.md") is True

    def test_releasing_opens_the_server_again(self, tmp_path):
        table = ToolAudience()
        table.restrict("srv", tmp_path / "plug")

        table.release("srv")

        assert table.allows("srv", None) is True

    def test_releasing_an_unknown_server_is_silent(self):
        ToolAudience().release("never-seen")

    def test_one_servers_restriction_does_not_touch_another(self, tmp_path):
        table = ToolAudience()
        table.restrict("srv", tmp_path / "plug")

        assert table.allows("other", None) is True


class _Client:
    initialized = True

    async def connect(self, *a, **k): ...
    async def close(self): ...


class TestTheBuilderReadsIt:
    def _tools_of(self, table, settings, source_path, mcp_clients):
        context = AgentBuildContext(
            settings=settings,
            mcp_clients=mcp_clients,
            supervisor=ProcessSupervisor(),
        )
        definition = AgentDefinition(
            name="a", description="d", tools=["Read"], source_path=source_path
        )
        with (
            patch.object(AgentBuilder, "_agent_cls") as agent_cls,
            patch.object(AgentBuilder, "_model_registry_cls"),
            patch.object(AgentBuilder, "_tool_registry_cls") as registry_cls,
        ):
            registry_cls.return_value.resolve.return_value = [object()]
            AgentBuilder(context, audience=table).build(definition)
        return agent_cls.call_args.kwargs["tools"]

    def test_a_restricted_server_is_withheld_from_an_outsider_and_given_to_an_insider(
        self, tmp_path, settings
    ):
        table = ToolAudience()
        table.restrict("vault", tmp_path / "plug")
        vault, open_srv = _Client(), _Client()
        clients = {"vault": vault, "open": open_srv}

        outsider = self._tools_of(table, settings, tmp_path / "mine" / "a.md", clients)
        insider = self._tools_of(table, settings, tmp_path / "plug" / "a.md", clients)
        main = self._tools_of(table, settings, None, clients)

        assert open_srv in outsider and vault not in outsider
        assert vault in insider and open_srv in insider
        assert open_srv in main and vault not in main


def test_the_builder_reads_the_module_table_when_given_none(settings):
    builder = AgentBuilder(AgentBuildContext(settings=settings, supervisor=ProcessSupervisor()))

    assert builder._audience is audience
