"""``/agents ephemeral`` lists the session's ephemeral agents.

The view once imported ``AgentDefinition`` only under
``TYPE_CHECKING``, so Pydantic could not resolve its field and every
``/agents ephemeral`` raised ``PydanticUserError`` instead of listing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from igni_core.backend.cmd_agents import AgentsCommand
from igni_core.core.agents.schemas import AgentDefinition
from igni_core.protocol.messages import CommandResultKind


def _command(*agents: AgentDefinition) -> AgentsCommand:
    pool = MagicMock()
    pool.has_ephemeral = True
    pool.ephemeral.list_agents.return_value = list(agents)
    return AgentsCommand(SimpleNamespace(pool=pool))


def test_ephemeral_agents_are_listed_with_their_tools():
    result = _command(
        AgentDefinition(name="migrator", description="moves data", tools=["read", "write"]),
        AgentDefinition(name="idle", description="does nothing"),
    ).list_ephemeral()

    assert result.kind == CommandResultKind.MARKDOWN
    assert "- **migrator** — moves data\n  tools: read, write\n" in result.content
    assert "- **idle** — does nothing\n  tools: none\n" in result.content


def test_no_ephemeral_agents_says_so():
    result = _command().list_ephemeral()

    assert result.kind == CommandResultKind.INFO
    assert result.content == "No ephemeral agents."
