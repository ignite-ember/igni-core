"""The docs describe nesting the way the spawn code bounds it.

AGENTS.md said sub-agents are capped at one level and can spawn nothing,
while AGENTS.md, ARCHITECTURE.md, COMPARISON.md, MIGRATION.md and the
project overview said nesting is unlimited (campaign R128). Neither is
true: an agent started with ``spawn_agent`` and marked ``can_orchestrate``
(the default) gets its own spawn tools, down to
``orchestration.max_nesting_depth`` (default 5).
"""

from __future__ import annotations

import re
from pathlib import Path

import igni_core
from igni_core.core.agents.schemas import AgentDefinition
from igni_core.core.config.schemas.orchestration import OrchestrationConfig

_ROOT = Path(__file__).resolve().parent.parent
_PAGES = [
    "AGENTS.md",
    "ARCHITECTURE.md",
    "COMPARISON.md",
    "MIGRATION.md",
    "IGNI_PROJECT_OVERVIEW.md",
]


def test_nesting_is_on_by_default_and_bounded_at_five():
    assert OrchestrationConfig().max_nesting_depth == 5
    assert AgentDefinition.model_fields["can_orchestrate"].default is True


def test_no_page_promises_unlimited_nesting():
    for page in _PAGES:
        text = (_ROOT / "docs" / page).read_text()
        assert not re.search(
            r"unlimited (agent )?nesting|no nesting limit|nesting is unlimited", text, re.I
        ), page
        assert "**Unlimited** — agents spawn" not in text, page


def test_agents_md_does_not_say_sub_agents_cannot_spawn():
    text = (_ROOT / "docs/AGENTS.md").read_text()
    assert "caps sub-agents at **one level**" not in text
    assert "a spawned agent has no way to spawn anything" not in text


def test_the_count_of_bundled_orchestrators_is_right():
    bundled = Path(igni_core.__file__).parent / "bundled_agents"
    marked = [p for p in bundled.glob("*.md") if "can_orchestrate: true" in p.read_text()]
    assert len(marked) == 7
    assert "seven of the eleven bundled agents" in (_ROOT / "docs/AGENTS.md").read_text()
