"""A text-only model reading a media file's content, as a pipeline stage."""

from __future__ import annotations

import logging

from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.outcome import Block, Continue, StageOutcome
from igni_core.core.hooks.permission.stage import PermissionStage
from igni_core.core.hooks.text_only_media_guard import TextOnlyMediaGuard
from igni_core.core.utils.tool_audit import DecidedBy

logger = logging.getLogger(__name__)


class VisionDisabledStage(PermissionStage):
    """Refuses a call that would read the content of a media file the turn named — see
    :mod:`~igni_core.core.hooks.text_only_media_guard`.

    Ahead of the evaluator, so nobody is asked to approve a call that is
    refused anyway. Always runs: a hook's ``allow`` does not give the model vision.
    """

    def __init__(self, guard: TextOnlyMediaGuard) -> None:
        self._guard = guard

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        message = self._guard.refusal(ctx.name, ctx.args)
        if message is None:
            return Continue()
        logger.info("Media read refused for a text-only model: %s", ctx.name)
        return Block(message, by=DecidedBy.VISION_DISABLED)
