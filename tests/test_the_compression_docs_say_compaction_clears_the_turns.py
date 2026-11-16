"""CONFIGURATION.md describes compaction as the coordinator does it.

It said compaction keeps recent turns and that later compactions halve
them (minimum 2). ``CompactionCoordinator.compact`` replaces every turn
with the summary (``agno_session.runs = []``) and keeps none (campaign
R077).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from igni_core.core.session.compaction.coordinator import CompactionCoordinator

_ROOT = Path(__file__).resolve().parent.parent


def test_the_coordinator_clears_every_turn():
    assert "agno_session.runs = []" in inspect.getsource(CompactionCoordinator.compact)


def test_the_page_promises_no_kept_or_halved_turns():
    text = (_ROOT / "docs/CONFIGURATION.md").read_text()
    assert "halve the kept turns" not in text
    assert "keep only recent turns" not in text
    assert "clears the verbatim history" in text
