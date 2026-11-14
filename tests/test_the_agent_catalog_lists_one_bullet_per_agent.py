"""The catalog the main agent's prompt carries: one markdown bullet per agent."""

from __future__ import annotations

from igni_core.core.agents.catalog import agent_catalog
from igni_core.core.agents.schemas import AgentDefinition


def _agent(name: str, description: str, tools: list[str] | None = None) -> AgentDefinition:
    return AgentDefinition(name=name, description=description, tools=tools or [])


def test_an_agent_is_a_bullet_with_its_description_and_tools() -> None:
    catalog = agent_catalog([_agent("coder", "Writes code", ["Write", "Edit", "Bash"])])
    assert catalog == "- **coder**: Writes code (tools: Write, Edit, Bash)"


def test_an_agent_without_tools_says_none() -> None:
    assert agent_catalog([_agent("talker", "Only talks")]) == (
        "- **talker**: Only talks (tools: none)"
    )


def test_no_agents_is_an_empty_string() -> None:
    assert agent_catalog([]) == ""


def test_agents_keep_their_order_one_per_line() -> None:
    catalog = agent_catalog([_agent("b", "second"), _agent("a", "first", ["Bash"])])
    assert catalog.split("\n") == [
        "- **b**: second (tools: none)",
        "- **a**: first (tools: Bash)",
    ]


def test_a_description_is_passed_through_untouched() -> None:
    catalog = agent_catalog([_agent("x", "Does *this*\nand `that`")])
    assert catalog == "- **x**: Does *this*\nand `that` (tools: none)"


def test_a_generator_is_read_once() -> None:
    agents = (_agent(f"a{i}", "d") for i in range(2))
    assert agent_catalog(agents).count("\n") == 1
