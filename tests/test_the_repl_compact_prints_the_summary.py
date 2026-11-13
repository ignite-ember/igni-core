"""``/compact`` in the plain-text REPL prints the model's summary, not the status line.

The command answers with the status line in ``content`` and the
summary in ``display_content``. The REPL labelled the status line
"Summary:" and dropped the real summary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from igni_core.core.session.commands import InteractiveCommandDispatcher
from igni_core.core.session.schemas import CompactResult

from .test_commands import _make_session


def _printed(display, method):
    return [call.args[0] for call in getattr(display, method).call_args_list]


@pytest.mark.asyncio
async def test_compact_prints_the_notice_and_the_summary_when_there_is_one() -> None:
    session = _make_session()
    session.compaction.force_compact = AsyncMock(
        return_value=CompactResult(ok=True, status="done", summary="WHAT HAPPENED")
    )
    await InteractiveCommandDispatcher(session).dispatch("/compact")
    assert _printed(session.display, "print_info") == [
        "Context compacted. Old messages summarized and cleared.",
        "Summary:\nWHAT HAPPENED",
    ]


@pytest.mark.asyncio
async def test_compact_with_no_summary_prints_only_the_notice() -> None:
    session = _make_session()
    session.compaction.force_compact = AsyncMock(
        return_value=CompactResult(ok=True, status="done", summary="")
    )
    await InteractiveCommandDispatcher(session).dispatch("/compact")
    assert _printed(session.display, "print_info") == [
        "Context compacted. Old messages summarized and cleared."
    ]
