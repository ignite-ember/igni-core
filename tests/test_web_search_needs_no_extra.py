"""WebSearch comes with igni-core; the docs do not send people to an extra.

TOOLS.md said WebSearch "needs the web extra; without it the name resolves
to nothing". It does not: igni-core depends on ``agno[ddg]``, which brings
the ``ddgs`` package agno's DuckDuckGo toolkit imports, so web search works
on a plain install. The ``web`` extra installs ``duckduckgo-search``, which
nothing imports (campaign R092).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_the_core_dependency_brings_the_search_package():
    pyproject = (_ROOT / "pyproject.toml").read_text()
    agno = re.search(r'"agno\[([^\]]*)\]', pyproject)
    assert agno is not None and "ddg" in agno.group(1).split(",")


def test_agnos_toolkit_imports_ddgs_not_duckduckgo_search():
    import agno.tools.duckduckgo as duckduckgo
    import agno.tools.websearch as websearch

    source = Path(duckduckgo.__file__).read_text() + Path(websearch.__file__).read_text()
    assert "from ddgs import" in source or "import ddgs" in source
    assert "duckduckgo_search" not in source


def test_tools_md_does_not_say_web_search_needs_the_extra():
    text = (_ROOT / "docs/TOOLS.md").read_text()
    rows = [ln for ln in text.splitlines() if ln.startswith("| `WebSearch`")]
    assert rows
    for row in rows:
        assert "web` extra" not in row and ".[web]" not in row, row


def test_quickstart_does_not_send_people_to_the_extra():
    text = (_ROOT / "QUICKSTART.md").read_text()
    section = text.split("### Web Tools", 1)[1].split("\n### ", 1)[0]
    assert ".[web]" not in section and "web]" not in section
