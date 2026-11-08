"""The shape every permission gate has."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.outcome import StageOutcome


class PermissionStage(ABC):
    """One gate of :class:`~igni_core.core.hooks.permission.pipeline.PermissionPipeline`."""

    #: Whether a ``PreToolUse`` hook's ``allow`` skips this stage. The
    #: safety lists say no: a hook cannot unlock a write to ``.env``.
    skipped_by_hook_allow: ClassVar[bool] = False

    @abstractmethod
    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome: ...
