"""``/agents``: what promote, discard and the catalogue say.

The wording is what a person reads in chat, so it is held exactly.
"""

from __future__ import annotations

from igni_core.backend.schemas_agents import DiscardResult, EphemeralAgentsView, PromoteResult
from igni_core.core.agents.schemas import AgentDefinition
from igni_core.protocol.messages import CommandResultKind


class TestPromote:
    def test_success_names_the_agent_and_where_it_went(self):
        result = PromoteResult(ok=True, destination="~/.igni/agents/scout.md").to_command_result(
            "scout"
        )

        assert result.kind == CommandResultKind.INFO
        assert result.content == "Promoted 'scout' to ~/.igni/agents/scout.md"

    def test_failure_says_why(self):
        result = PromoteResult(ok=False, error="already exists").to_command_result("scout")

        assert result.kind == CommandResultKind.ERROR
        assert result.content == "already exists"

    def test_failure_without_a_reason_says_unknown_error(self):
        result = PromoteResult(ok=False).to_command_result("scout")

        assert result.kind == CommandResultKind.ERROR
        assert result.content == "unknown error"


class TestDiscard:
    def test_success_names_the_agent(self):
        result = DiscardResult(ok=True).to_command_result("scout")

        assert result.kind == CommandResultKind.INFO
        assert result.content == "Discarded ephemeral agent 'scout'."

    def test_failure_says_why(self):
        result = DiscardResult(ok=False, error="no such agent").to_command_result("scout")

        assert result.kind == CommandResultKind.ERROR
        assert result.content == "no such agent"

    def test_failure_without_a_reason_says_unknown_error(self):
        result = DiscardResult(ok=False).to_command_result("scout")

        assert result.kind == CommandResultKind.ERROR
        assert result.content == "unknown error"


class TestTheCatalogue:
    def test_no_agents_is_an_info_line(self):
        result = EphemeralAgentsView(agents=[]).to_command_result()

        assert result.kind == CommandResultKind.INFO
        assert result.content == "No ephemeral agents."

    def test_each_agent_has_its_name_description_and_tools(self):
        view = EphemeralAgentsView(
            agents=[
                AgentDefinition(name="scout", description="Finds files", tools=["grep", "read"]),
                AgentDefinition(name="writer", description="Writes docs", tools=[]),
            ]
        )

        result = view.to_command_result()

        assert result.kind == CommandResultKind.MARKDOWN
        assert result.content == (
            "## Ephemeral Agents\n"
            "- **scout** — Finds files\n"
            "  tools: grep, read\n"
            "- **writer** — Writes docs\n"
            "  tools: none\n"
            "\n*`/agents promote <name>` to save · `/agents discard <name>` to remove*\n"
        )

    def test_markdown_in_a_name_or_description_is_passed_through_unescaped(self):
        view = EphemeralAgentsView(
            agents=[AgentDefinition(name="a_b", description="*bold* `code`")]
        )

        assert "- **a_b** — *bold* `code`\n" in view.to_command_result().content
