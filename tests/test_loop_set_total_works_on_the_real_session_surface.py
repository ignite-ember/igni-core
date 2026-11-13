"""``loop_set_total`` against what the real ``Session`` offers.

The tool called ``session.set_announced_total_from_tool`` — a method the
session never had (its ``loop`` controller and ``loop_progress_store``
are what it declares). The test sessions faked the method, so the model
calling ``loop_set_total`` in a real session got an ``AttributeError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from igni_core.core.loop.store import LoopProgressStore
from igni_core.core.session.loop_ops import LoopController
from igni_core.core.session.surface import SessionSurface
from igni_core.core.tools.loop import LoopTools


class _SessionLikeTheRealOne:
    """Offers exactly what :class:`Session` declares: ``loop`` and ``loop_progress_store``."""

    def __init__(self, store: Any = None) -> None:
        self.loop = LoopController(loop_store=AsyncMock())
        self.loop_progress_store = store if store is not None else AsyncMock()


def test_the_session_surface_declares_what_the_loop_tools_reach_for() -> None:
    declared = SessionSurface.__annotations__
    assert "loop" in declared and "loop_progress_store" in declared
    assert not hasattr(SessionSurface, "set_announced_total_from_tool")


async def test_loop_set_total_reaches_the_controller_and_the_progress_store(
    tmp_path: Path,
) -> None:
    store = LoopProgressStore(db_path=tmp_path / "state.db")
    session = _SessionLikeTheRealOne(store)
    await session.loop.start_loop("walk the files", 10, immediate=False, cap_explicit=True)
    run_id = session.loop.loop_run_id
    assert run_id

    said = await LoopTools(session).loop_set_total(12)  # type: ignore[arg-type]

    assert "12" in said
    assert await store.get(run_id, LoopProgressStore.ANNOUNCED_TOTAL_KEY) == "12"


async def test_loop_set_total_without_a_loop_is_refused_on_the_real_surface() -> None:
    said = await LoopTools(_SessionLikeTheRealOne()).loop_set_total(3)  # type: ignore[arg-type]
    assert said.startswith("ERROR")
    assert "no loop" in said.lower()
