"""Telling somebody at startup, not mid-task.

An agent naming a tool igni does not have loads without complaint and
builds without complaint. It raises when something calls it — which is
whoever happened to invoke it, doing something else, possibly weeks
after the name was written.

The check runs once the pools are built. That timing is the point:
custom Python tools register when they are discovered, so asking any
earlier would call somebody's own tool a typo. Two things it must never
do — treat an MCP name as a mistake, or stop a session from starting.
"""

from __future__ import annotations

from igni_core.core.agents.tool_check import UnresolvedAgentTools
from igni_core.core.session.parts.agents import AgentsBuilder
from igni_core.core.tools.registry import ToolRegistry
from igni_core.core.tools.tool_spec import ToolResolutionRequest


class TestWhatResolvesAndWhatDoesNot:
    def test_a_real_tool_resolves(self, tmp_path):
        result = ToolRegistry(base_dir=str(tmp_path)).resolve_typed(
            ToolResolutionRequest(tool_names=["Bash", "Write"])
        )

        assert result.unknown == []

    def test_a_typo_is_reported_rather_than_raised(self, tmp_path):
        """``resolve`` raises; the diagnostic path must not, or checking
        would be as disruptive as the failure it is warning about."""
        result = ToolRegistry(base_dir=str(tmp_path)).resolve_typed(
            ToolResolutionRequest(tool_names=["WebSarch", "Bash"])
        )

        assert result.unknown == ["WebSarch"]

    def test_an_mcp_name_is_not_a_typo(self, tmp_path):
        """Those connect after the registry is built, so nothing here
        can know them."""
        result = ToolRegistry(base_dir=str(tmp_path)).resolve_typed(
            ToolResolutionRequest(tool_names=["MCP:github"])
        )

        assert result.unknown == []


class TestThePoolCheck:
    """Driven against the real registry — the answer is only as good as
    what it can resolve."""

    def _check(self, tmp_path, agents) -> UnresolvedAgentTools:
        return UnresolvedAgentTools.check(agents, ToolRegistry(base_dir=str(tmp_path)))

    @staticmethod
    def _agent(name: str, tools: list[str]):
        return type("Defn", (), {"name": name, "tools": tools})()

    def test_a_clean_agent_reports_nothing(self, tmp_path):
        check = self._check(tmp_path, [self._agent("reviewer", ["Bash", "Write"])])

        assert check.by_agent() == {}

    def test_a_typo_is_named_with_its_agent(self, tmp_path):
        check = self._check(tmp_path, [self._agent("reviewer", ["WebSarch", "Bash"])])

        assert check.by_agent() == {"reviewer": ["WebSarch"]}

    def test_an_agent_with_no_tools_is_skipped(self, tmp_path):
        check = self._check(tmp_path, [self._agent("thinker", [])])

        assert check.by_agent() == {}

    def test_it_logs_what_is_available(self, tmp_path, caplog):
        """The fix is usually visible in the list."""
        check = self._check(tmp_path, [self._agent("reviewer", ["WebSarch"])])

        with caplog.at_level("WARNING"):
            check.log_warnings()

        assert "WebSarch" in caplog.text
        assert "WebSearch" in caplog.text  # in the available list

    def test_a_broken_check_does_not_stop_a_session(self, tmp_path):
        """It is a diagnostic. Failing to produce it must cost the
        diagnostic, not the session."""
        from igni_core.core.session.core import Session

        class Exploding:
            def list_agents(self):
                raise RuntimeError("no pool")

        session = Session.__new__(Session)
        session.project_dir = tmp_path
        session.pool = Exploding()

        assert AgentsBuilder(session).check_tools().by_agent() == {}
