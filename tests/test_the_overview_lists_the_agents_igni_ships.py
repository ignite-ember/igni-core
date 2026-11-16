"""The project overview names the agents igni ships, and no others.

It said "10 base agents (architect, data-architect, ...) + 6
``.codeindex.md`` variants" and "13 .md files + 6 .codeindex variants",
and gave a data-architect agent the CodeIndex tool. The package ships
eleven agents, none of them data-architect or a codeindex variant
(campaign R127).
"""

from __future__ import annotations

from pathlib import Path

import igni_core

_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED = Path(igni_core.__file__).parent / "bundled_agents"


def test_every_bundled_agent_is_named_and_no_other():
    text = (_ROOT / "docs/IGNI_PROJECT_OVERVIEW.md").read_text()
    names = sorted(p.stem for p in _BUNDLED.glob("*.md"))
    assert len(names) == 11
    row = next(ln for ln in text.splitlines() if ln.startswith("| **Built-in specialist agents**"))
    for name in names:
        assert name in row, name
    for stale in ("data-architect", ".codeindex.md", "13 .md files", "10 base agents"):
        assert stale not in text, stale


def test_no_codeindex_agent_ships():
    assert not list(_BUNDLED.glob("*codeindex*"))
