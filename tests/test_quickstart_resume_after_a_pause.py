"""QUICKSTART's Resume Sessions says a paused turn is not carried over.

A session whose only turn paused (`igni -m` exit 1) resumed with
--session-id answered "this is the first message of our conversation":
agno leaves paused runs out of the history it rebuilds (their tool call
has no result to send back), and nothing said so.
"""

from __future__ import annotations

import pathlib

from agno.run.base import HISTORY_SKIP_STATUSES, RunStatus

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _resume_sessions() -> str:
    text = (_ROOT / "QUICKSTART.md").read_text(encoding="utf-8")
    start = text.index("## Resume Sessions")
    return text[start : text.index("\n## ", start + 1)]


def test_a_paused_run_is_left_out_of_the_history_agno_rebuilds():
    assert RunStatus.paused in HISTORY_SKIP_STATUSES


def test_resume_sessions_says_a_paused_turn_is_not_in_the_history():
    section = _resume_sessions()

    assert "paused for confirmation" in section
    assert "not part of the session's history" in section
