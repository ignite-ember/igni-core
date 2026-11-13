"""Tests for session/memory_ops.py — user memory management."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.memory import MemoryManager, UserMemory

from igni_core.core.config.settings import Settings
from igni_core.core.session.memory_ops import (
    MemoryEntry,
    MemoryOptimizeResult,
    MemoryReadResult,
    SessionMemoryManager,
)


def _reader_agent(
    memories: list[UserMemory] | None = None, error: Exception | None = None
) -> MagicMock:
    """A stand-in for the Agno reader agent.

    ``memories`` is what ``aget_user_memories`` returns; ``error`` is
    what it raises instead. Only the one seam under test is faked.
    """
    agent = MagicMock()
    if error is not None:
        agent.aget_user_memories = AsyncMock(side_effect=error)
    else:
        agent.aget_user_memories = AsyncMock(return_value=memories)
    return agent


class TestSessionMemoryManager:
    def test_create_manager_no_db(self):
        settings = Settings()
        mgr = SessionMemoryManager(db=None, settings=settings, user_id="test-user")
        result = mgr._create_manager()
        assert result is None

    def test_create_manager_with_db(self):
        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="test-user")
        assert isinstance(mgr._create_manager(), MemoryManager)

    @pytest.mark.asyncio
    async def test_get_memories_no_db(self):
        settings = Settings()
        mgr = SessionMemoryManager(db=None, settings=settings, user_id="user")
        result = await mgr.get_memories()
        assert result.success
        assert result.error is None
        assert result.memories == []

    @pytest.mark.asyncio
    async def test_get_memories_returns_typed_entries(self):
        mem = UserMemory(memory="User prefers concise responses", topics=["preferences", "style"])

        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with patch.object(mgr, "_create_reader_agent", return_value=_reader_agent([mem])):
            result = await mgr.get_memories()

        assert result.success
        assert result.memories == [
            MemoryEntry(memory="User prefers concise responses", topics="preferences, style")
        ]

    @pytest.mark.asyncio
    async def test_get_memories_without_topics(self):
        mem = UserMemory(memory="User is a backend engineer")

        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with patch.object(mgr, "_create_reader_agent", return_value=_reader_agent([mem])):
            result = await mgr.get_memories()

        assert result.success
        assert result.memories == [MemoryEntry(memory="User is a backend engineer", topics="")]

    @pytest.mark.asyncio
    async def test_get_memories_empty_store(self):
        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with patch.object(mgr, "_create_reader_agent", return_value=_reader_agent([])):
            result = await mgr.get_memories()

        assert result.success
        assert result.memories == []

    @pytest.mark.asyncio
    async def test_get_memories_reports_failure(self):
        """A failed read is not the same answer as "no memories"."""
        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with patch.object(
            mgr,
            "_create_reader_agent",
            return_value=_reader_agent(error=RuntimeError("fail")),
        ):
            result = await mgr.get_memories()

        assert not result.success
        assert result.error is not None
        assert "fail" in result.error
        assert result.memories == []

    @pytest.mark.asyncio
    async def test_optimize_no_manager(self):
        settings = Settings()
        mgr = SessionMemoryManager(db=None, settings=settings, user_id="user")
        result = await mgr.optimize()
        assert not result.success
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_optimize_not_enough_memories(self):
        mock_agent = _reader_agent([UserMemory(memory="one")])
        mock_manager = MagicMock()

        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with (
            patch.object(mgr, "_create_manager", return_value=mock_manager),
            patch.object(mgr, "_create_reader_agent", return_value=mock_agent),
        ):
            result = await mgr.optimize()

        assert result.success
        assert result.count_before == 1
        assert "Not enough" in result.message
        mock_manager.aoptimize_memories.assert_not_called()

    @pytest.mark.asyncio
    async def test_optimize_success(self):
        mock_agent = _reader_agent([UserMemory(memory="one"), UserMemory(memory="two")])
        mock_manager = MagicMock()
        mock_manager.aoptimize_memories = AsyncMock(return_value=[UserMemory(memory="merged")])

        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with (
            patch.object(mgr, "_create_manager", return_value=mock_manager),
            patch.object(mgr, "_create_reader_agent", return_value=mock_agent),
        ):
            result = await mgr.optimize()

        assert result.success
        assert result.error is None
        assert result.count_before == 2
        assert result.count_after == 1
        assert "Optimized 2 memories into 1" in result.message

    @pytest.mark.asyncio
    async def test_optimize_reports_failure(self):
        mock_agent = _reader_agent([UserMemory(memory="one"), UserMemory(memory="two")])
        mock_manager = MagicMock()
        mock_manager.aoptimize_memories = AsyncMock(side_effect=RuntimeError("boom"))

        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with (
            patch.object(mgr, "_create_manager", return_value=mock_manager),
            patch.object(mgr, "_create_reader_agent", return_value=mock_agent),
        ):
            result = await mgr.optimize()

        assert not result.success
        assert result.error == "boom"

    @pytest.mark.asyncio
    async def test_optimize_reports_read_failure(self):
        mock_agent = _reader_agent(error=RuntimeError("read failed"))
        mock_manager = MagicMock()

        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with (
            patch.object(mgr, "_create_manager", return_value=mock_manager),
            patch.object(mgr, "_create_reader_agent", return_value=mock_agent),
        ):
            result = await mgr.optimize()

        assert not result.success
        assert result.error == "read failed"
        mock_manager.aoptimize_memories.assert_not_called()


class TestMemoryResults:
    def test_read_result_ok(self):
        result = MemoryReadResult.ok([MemoryEntry(memory="one")])
        assert result.success
        assert result.error is None
        assert len(result.memories) == 1

    def test_read_result_fail(self):
        result = MemoryReadResult.fail("nope")
        assert not result.success
        assert result.error == "nope"
        assert result.memories == []

    def test_optimize_result_ok(self):
        result = MemoryOptimizeResult.ok(before=2, after=1, message="done")
        assert result.success
        assert result.count_before == 2
        assert result.count_after == 1
        assert result.message == "done"

    def test_optimize_result_fail(self):
        result = MemoryOptimizeResult.fail("nope")
        assert not result.success
        assert result.error == "nope"


class TestWhatTheManagerAsksAgno:
    """The arguments the manager hands Agno are the behaviour a user sees:
    whose memories are read, and how they are optimized."""

    @pytest.mark.asyncio
    async def test_the_session_users_memories_are_the_ones_read(self):
        agent = _reader_agent([])
        mgr = SessionMemoryManager(db=MagicMock(), settings=Settings(), user_id="alice")

        with patch.object(mgr, "_create_reader_agent", return_value=agent):
            await mgr.get_memories()

        agent.aget_user_memories.assert_awaited_once_with(user_id="alice")

    @pytest.mark.asyncio
    async def test_a_reader_that_returns_nothing_is_an_empty_store(self):
        mgr = SessionMemoryManager(db=MagicMock(), settings=Settings(), user_id="u")

        with patch.object(mgr, "_create_reader_agent", return_value=_reader_agent(None)):
            result = await mgr.get_memories()

        assert result.success
        assert result.memories == []

    @pytest.mark.asyncio
    async def test_a_failed_read_names_what_could_not_be_read(self):
        mgr = SessionMemoryManager(db=MagicMock(), settings=Settings(), user_id="u")

        with patch.object(
            mgr, "_create_reader_agent", return_value=_reader_agent(error=RuntimeError("db down"))
        ):
            result = await mgr.get_memories()

        assert result.error == "Could not read user memories: db down"

    @pytest.mark.asyncio
    async def test_a_memory_without_text_reads_as_empty_and_topics_are_joined_in_order(self):
        mem = [
            UserMemory(memory=None, topics=["b", "a", "é, ü"]),  # type: ignore[arg-type]
            UserMemory(memory="世界", topics=[]),
        ]
        mgr = SessionMemoryManager(db=MagicMock(), settings=Settings(), user_id="u")

        with patch.object(mgr, "_create_reader_agent", return_value=_reader_agent(mem)):
            result = await mgr.get_memories()

        assert result.memories == [
            MemoryEntry(memory="", topics="b, a, é, ü"),
            MemoryEntry(memory="世界", topics=""),
        ]

    @pytest.mark.asyncio
    async def test_optimizing_summarizes_and_applies_for_the_session_user(self):
        from agno.memory.strategies.types import MemoryOptimizationStrategyType

        manager = MagicMock()
        manager.aoptimize_memories = AsyncMock(return_value=[UserMemory(memory="m")])
        mgr = SessionMemoryManager(db=MagicMock(), settings=Settings(), user_id="alice")
        two = _reader_agent([UserMemory(memory="a"), UserMemory(memory="b")])

        with (
            patch.object(mgr, "_create_manager", return_value=manager),
            patch.object(mgr, "_create_reader_agent", return_value=two),
        ):
            await mgr.optimize()

        manager.aoptimize_memories.assert_awaited_once_with(
            user_id="alice", strategy=MemoryOptimizationStrategyType.SUMMARIZE, apply=True
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("optimized", [None, []])
    async def test_an_optimizer_that_returns_nothing_leaves_zero_memories(self, optimized):
        manager = MagicMock()
        manager.aoptimize_memories = AsyncMock(return_value=optimized)
        mgr = SessionMemoryManager(db=MagicMock(), settings=Settings(), user_id="u")
        two = _reader_agent([UserMemory(memory="a"), UserMemory(memory="b")])

        with (
            patch.object(mgr, "_create_manager", return_value=manager),
            patch.object(mgr, "_create_reader_agent", return_value=two),
        ):
            result = await mgr.optimize()

        assert result.success
        assert (result.count_before, result.count_after) == (2, 0)
        assert result.message == "Optimized 2 memories into 0"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", [0, 1])
    async def test_below_two_memories_nothing_is_optimized(self, stored):
        manager = MagicMock()
        manager.aoptimize_memories = AsyncMock()
        mgr = SessionMemoryManager(db=MagicMock(), settings=Settings(), user_id="u")
        agent = _reader_agent([UserMemory(memory="x")] * stored)

        with (
            patch.object(mgr, "_create_manager", return_value=manager),
            patch.object(mgr, "_create_reader_agent", return_value=agent),
        ):
            result = await mgr.optimize()

        assert (result.count_before, result.count_after) == (stored, stored)
        assert result.message == "Not enough memories to optimize"
        manager.aoptimize_memories.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_database_means_the_manager_is_not_available(self):
        mgr = SessionMemoryManager(db=None, settings=Settings(), user_id="u")

        result = await mgr.optimize()

        assert result.error == "Memory manager not available"

    def test_the_reader_agent_is_named_and_bound_to_the_sessions_database(self):
        db = MagicMock()
        mgr = SessionMemoryManager(db=db, settings=Settings(), user_id="u")

        with (
            patch.object(mgr, "_model", return_value=MagicMock()),
            patch("igni_core.core.session.memory_ops.Agent") as agent_cls,
        ):
            mgr._create_reader_agent()

        kwargs = agent_cls.call_args.kwargs
        assert kwargs["name"] == "_memory_reader"
        assert kwargs["db"] is db
