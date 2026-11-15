"""The learning and orchestration defaults the docs give are the code's.

CONFIGURATION.md gave ``learning.user_profile: true`` and
``session_context: true`` as defaults; both are False (campaign R138).
``orchestration.sub_team_timeout`` was 600 in CONFIGURATION.md, 120 in
AGENTS.md and SECURITY.md, and 1800 in the code (campaign R128).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from igni_core.core.config.settings import Settings

_DOCS = Path(__file__).resolve().parent.parent / "docs"


def _reference_blocks() -> list[tuple[str, dict]]:
    """The YAML blocks that list a learning or orchestration block's keys
    with their defaults — not the short "raise it to …" examples."""
    found: list[tuple[str, dict]] = []
    for name in ("CONFIGURATION.md", "SECURITY.md"):
        for raw in re.findall(r"```yaml\n(.*?)```", (_DOCS / name).read_text(), re.S):
            try:
                parsed = yaml.safe_load(raw)
            except yaml.YAMLError:
                continue
            if not isinstance(parsed, dict):
                continue
            for block in ("learning", "orchestration"):
                values = parsed.get(block)
                if isinstance(values, dict) and len(values) >= 3:
                    found.append((f"{name}:{block}", {block: values}))
    return found


def test_the_reference_blocks_were_found():
    assert len(_reference_blocks()) >= 3


@pytest.mark.parametrize(("where", "documented"), _reference_blocks())
def test_each_documented_value_is_the_default(where: str, documented: dict):
    defaults = Settings().model_dump()
    for block, values in documented.items():
        for key, value in values.items():
            assert key in defaults[block], f"{where}: {block}.{key} is not a setting"
            assert defaults[block][key] == value, (
                f"{where} says {block}.{key}: {value!r}; the default is {defaults[block][key]!r}"
            )


def test_the_agents_page_safeguard_table_gives_the_default_timeout():
    row = next(
        ln for ln in (_DOCS / "AGENTS.md").read_text().splitlines() if "| `sub_team_timeout`" in ln
    )
    assert f"{Settings().orchestration.sub_team_timeout} seconds" in row
