"""The docs name only the ``/agents`` subcommands the command has.

docs/TOOLS.md said "``create_agent`` and ``/agents create`` go through
``ToolRegistry.normalize_agno_names``". There is no ``create`` verb:
``/agents create foo`` opens the Agents panel and creates nothing, and an
ephemeral agent is made only by the model's ``create_agent`` tool
(campaign R131).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VERBS = re.findall(
    r'case "(\w+)":', (_ROOT / "src/igni_core/backend/cmd_agents.py").read_text()
)


def test_the_command_has_its_documented_verbs():
    assert {"promote", "discard", "ephemeral"} <= set(_VERBS)


def test_no_doc_names_a_verb_the_command_lacks():
    for doc in [*(_ROOT / "docs").glob("*.md"), *_ROOT.glob("*.md")]:
        for verb in re.findall(r"(?:^|[\s`])/agents (\w+)", doc.read_text(), re.M):
            if verb.islower() and verb not in {"promote", "discard", "ephemeral"}:
                assert verb in _VERBS, f"{doc.name} names /agents {verb}"
