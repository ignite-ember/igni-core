"""``/memory``'s other verbs: optimize, the disabled notice, the recall call.

What ``/memory`` shows when it recalls is pinned in
``test_memory_shows_what_was_learned.py``; this is the rest of the
command's contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.cmd_memory import cmd_memory
from igni_core.core.session.memory_ops import MemoryOptimizeResult
from igni_core.protocol.schemas.enums import CommandResultKind


class _Manager:
    def __init__(self, result: MemoryOptimizeResult):
        self._result = result
        self.calls = 0

    async def optimize(self) -> MemoryOptimizeResult:
        self.calls += 1
        return self._result


class _Learning:
    def __init__(self):
        self.calls: list[dict] = []

    async def arecall(self, **kwargs):
        self.calls.append(kwargs)
        return {}


def _handler(*, manager=None, learning=None):
    session = SimpleNamespace(
        memory_mgr=manager,
        learning_mgr=SimpleNamespace(effective_machine=learning),
        user_id="u-7",
    )
    return SimpleNamespace(session=session)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["optimize", "OPTIMIZE", "  Optimize "])
async def test_optimize_returns_the_manager_s_message(args: str):
    manager = _Manager(MemoryOptimizeResult.ok(before=9, after=4, message="Compacted 9 to 4."))
    result = await cmd_memory(_handler(manager=manager), args)
    assert result.kind is CommandResultKind.INFO
    assert result.content == "Compacted 9 to 4."
    assert manager.calls == 1


@pytest.mark.asyncio
async def test_a_failed_optimize_is_an_error_with_the_reason():
    manager = _Manager(MemoryOptimizeResult.fail("db locked"))
    result = await cmd_memory(_handler(manager=manager), "optimize")
    assert result.kind is CommandResultKind.ERROR
    assert result.content == "Memory optimization failed: db locked"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "optimise", "whatever"])
async def test_anything_but_optimize_shows_learnings_and_never_optimizes(args: str):
    manager = _Manager(MemoryOptimizeResult.ok(before=0, after=0, message="x"))
    learning = _Learning()
    await cmd_memory(_handler(manager=manager, learning=learning), args)
    assert manager.calls == 0
    assert len(learning.calls) == 1


@pytest.mark.asyncio
async def test_with_learning_off_it_says_how_to_turn_it_on():
    result = await cmd_memory(_handler(learning=None), "")
    assert result.kind is CommandResultKind.INFO
    assert "learning.enabled=true" in result.content


@pytest.mark.asyncio
async def test_recall_is_for_the_user_across_sessions():
    """No ``session_id``: a session-scoped recall would hide what was
    learned in earlier sessions, which is what this command is for."""
    learning = _Learning()
    await cmd_memory(_handler(learning=learning), "")
    assert learning.calls == [{"user_id": "u-7"}]
