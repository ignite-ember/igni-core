"""AGENTS.md's "Built-in Agents" lists the agents igni ships, as they ship.

The page listed nine of eleven (no ``plan_researcher``, no ``visualizer``)
and showed ``reasoning: true`` for architect, reviewer, security and
debugger, which all ship ``reasoning: false``. COMPARISON.md counted a
``data-architect`` that does not exist (campaign R127).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED = _ROOT / "src/igni_core/bundled_agents"
_PAGE = _ROOT / "docs/AGENTS.md"


def _frontmatter(path: Path) -> dict:
    text = path.read_text()
    return yaml.safe_load(text.split("---", 2)[1]) or {}


def _section() -> str:
    text = _PAGE.read_text()
    return text[text.index("### Built-in Agents") : text.index("## The Orchestrator")]


def _documented() -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for name, block in re.findall(
        r"\*\*(\w+)\.md\*\* —[^\n]*\n```yaml\n(.*?)```", _section(), re.S
    ):
        entries[name] = yaml.safe_load(block) or {}
    return entries


def _tools(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(v).strip() for v in value}
    return {t.strip() for t in str(value).split(",") if t.strip()}


_AGENTS = sorted(p.stem for p in _BUNDLED.glob("*.md"))


def test_the_bundled_agents_were_found():
    assert len(_AGENTS) >= 10


@pytest.mark.parametrize("agent", _AGENTS)
def test_each_bundled_agent_has_an_entry_that_matches_its_file(agent: str):
    documented = _documented()
    assert agent in documented, f"{agent} ships and has no entry under Built-in Agents"
    shipped = _frontmatter(_BUNDLED / f"{agent}.md")
    entry = documented[agent]
    assert _tools(entry.get("tools")) == _tools(shipped.get("tools")), agent
    assert bool(entry.get("reasoning", False)) == bool(shipped.get("reasoning", False)), agent


def test_no_entry_names_an_agent_igni_does_not_ship():
    assert set(_documented()) <= set(_AGENTS)


def test_the_comparison_counts_no_agent_that_does_not_ship():
    assert "data-architect" not in (_ROOT / "docs/COMPARISON.md").read_text()
