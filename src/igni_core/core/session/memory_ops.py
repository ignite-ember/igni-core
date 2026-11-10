"""Session memory operations — reading and optimizing user memories.

Two operations live here, both on :class:`SessionMemoryManager`:

* :meth:`SessionMemoryManager.get_memories` — read the user memories
  Agno stores for the session's user.
* :meth:`SessionMemoryManager.optimize` — compact those memories with
  Agno's summarize strategy.

Both return a Pydantic result model (Pattern 3). An expected failure —
no database, a read that blew up — comes back as ``success=False`` plus
an ``error`` string. Nothing raises for an expected failure, and a
failed read never masquerades as "the user has no memories": the
caller can tell the two apart.
"""

from __future__ import annotations

import logging
from typing import Self

from agno.agent import Agent
from agno.db.base import AsyncBaseDb
from agno.memory import MemoryManager, UserMemory
from agno.memory.strategies.types import MemoryOptimizationStrategyType
from agno.models.base import Model
from pydantic import BaseModel, Field

from igni_core.core.agno_telemetry import AgnoTelemetry
from igni_core.core.config.models import ModelRegistry
from igni_core.core.config.settings import Settings

logger = logging.getLogger(__name__)


class MemoryEntry(BaseModel):
    """One stored user memory, flattened for display.

    Rule 1: a memory is a two-field structured value, so it travels as
    a model — not a dict literal assembled at the callsite.

    ``topics`` is the rendered form of Agno's topic list: the stored
    topics joined with ``", "`` (an empty string when the memory
    carries none).
    """

    memory: str
    topics: str = ""


class MemoryOpResult(BaseModel):
    """Base result for a memory operation (Pattern 3).

    Expected failures are values, not exceptions: ``success`` says
    whether the operation worked and ``error`` carries the reason when
    it did not. Only bugs raise.
    """

    success: bool = True
    error: str | None = None

    @classmethod
    def fail(cls, error: str) -> Self:
        """Build a failed result carrying ``error``."""
        return cls(success=False, error=error)


class MemoryReadResult(MemoryOpResult):
    """Result of :meth:`SessionMemoryManager.get_memories`."""

    memories: list[MemoryEntry] = Field(default_factory=list)

    @classmethod
    def ok(cls, memories: list[MemoryEntry]) -> Self:
        """Build a successful result carrying ``memories``."""
        return cls(success=True, memories=memories)


class MemoryOptimizeResult(MemoryOpResult):
    """Result of :meth:`SessionMemoryManager.optimize`.

    Rule 1 / Pattern 3: callers check ``success`` and read the typed
    fields rather than probing for ``"error"`` keys on a raw dict.
    """

    message: str = ""
    count_before: int = 0
    count_after: int = 0

    @classmethod
    def ok(cls, *, before: int, after: int, message: str) -> Self:
        """Build a successful result with the before/after counts."""
        return cls(success=True, message=message, count_before=before, count_after=after)


class SessionMemoryManager:
    """Manages Agno-backed user memories for a session."""

    def __init__(self, db: AsyncBaseDb | None, settings: Settings, user_id: str) -> None:
        self.db = db
        self.settings = settings
        self.user_id = user_id

    def _model(self) -> Model:
        """Resolve the configured model used for memory operations."""
        return ModelRegistry(self.settings).get_model()

    def _create_manager(self) -> MemoryManager | None:
        """Create an Agno MemoryManager for memory operations."""
        if not self.db:
            return None
        return MemoryManager(model=self._model(), db=self.db)

    def _create_reader_agent(self) -> Agent:
        """Create a temporary agent for reading memories."""
        return Agent(
            name="_memory_reader",
            model=self._model(),
            db=self.db,
            telemetry=AgnoTelemetry.CONSTRUCTOR_VALUE,
        )

    async def _read_user_memories(self) -> list[UserMemory]:
        """Read the raw Agno memories stored for this session's user.

        Raises whatever the Agno read raises — both callers turn that
        into a failed result, so the handling lives with the callers
        rather than being swallowed here.
        """
        agent = self._create_reader_agent()
        memories = await agent.aget_user_memories(user_id=self.user_id)
        return list(memories or [])

    async def get_memories(self) -> MemoryReadResult:
        """Get all user memories for the current user."""
        if not self.db:
            return MemoryReadResult.ok([])
        try:
            memories = await self._read_user_memories()
        except Exception as exc:  # noqa: BLE001 — reported as a failed result
            logger.warning("Failed to get user memories: %s", exc)
            return MemoryReadResult.fail(f"Could not read user memories: {exc}")
        return MemoryReadResult.ok(
            [MemoryEntry(memory=m.memory or "", topics=", ".join(m.topics or [])) for m in memories]
        )

    async def optimize(self) -> MemoryOptimizeResult:
        """Optimize user memories using the summarize strategy."""
        manager = self._create_manager()
        if manager is None:
            return MemoryOptimizeResult.fail("Memory manager not available")
        try:
            count_before = len(await self._read_user_memories())

            if count_before < 2:
                return MemoryOptimizeResult.ok(
                    before=count_before,
                    after=count_before,
                    message="Not enough memories to optimize",
                )

            optimized = await manager.aoptimize_memories(
                user_id=self.user_id,
                strategy=MemoryOptimizationStrategyType.SUMMARIZE,
                apply=True,
            )
            count_after = len(optimized) if optimized else 0

            return MemoryOptimizeResult.ok(
                before=count_before,
                after=count_after,
                message=f"Optimized {count_before} memories into {count_after}",
            )
        except Exception as exc:  # noqa: BLE001 — reported as a failed result
            logger.warning("Memory optimization failed: %s", exc)
            return MemoryOptimizeResult.fail(str(exc))
