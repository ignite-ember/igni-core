"""The docs say igni loads only the agent directories its loader scans.

CONFIGURATION.md, AGENTS.md and ARCHITECTURE.md said that with
``cross_tool_support`` igni also reads Codex agents — ``AGENTS.md`` /
``.codex/`` in a project and ``~/.codex/``. The loader has no Codex code:
neither a project's AGENTS.md nor ``~/.codex/config.toml`` ever reached
the prompt or the pool (campaign R082, R132).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def test_the_loader_knows_no_codex_source():
    source = (_ROOT / "src/igni_core/core/agents/loader.py").read_text().lower()
    assert "codex" not in source


@pytest.mark.parametrize("page", ["CONFIGURATION.md", "AGENTS.md", "ARCHITECTURE.md"])
def test_the_page_does_not_say_codex_agents_load(page: str):
    lines = (_ROOT / "docs" / page).read_text().splitlines()
    claims = [ln for ln in lines if ".codex/" in ln and "not read" not in ln]
    assert claims == [], f"docs/{page} still lists a Codex directory as scanned: {claims}"
