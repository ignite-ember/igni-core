"""``/loop`` + compaction + learning RPC handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from igni_core.backend.rpc_handlers.base import RpcHandler, WireArgs, rpc
from igni_core.backend.schemas_loop import LoopStatusSnapshot
from igni_core.core.session.schemas import LoopAdvance
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class CompactIfNeededArgs(BaseModel):
    """``compact_if_needed``: the turn's context size against its window."""

    model_config = ConfigDict(extra="ignore")

    ctx_tokens: int
    max_ctx: int


class ExtractLearningsArgs(BaseModel):
    """``extract_learnings``: the turn's user and assistant messages."""

    model_config = ConfigDict(extra="ignore")

    user_msg: str
    assistant_msg: str


class LoopRpcHandler(RpcHandler):
    """Loop iteration control (pop/cancel/status/resume/pause) plus
    the compaction + learning extraction RPCs that ride on the same
    turn boundary."""

    @rpc(RpcMethod.POP_PENDING_LOOP_ITERATION)
    async def loop_pop(self, args: WireArgs) -> LoopAdvance | None:
        return await self._controllers.loop.pop_pending_iteration()

    @rpc(RpcMethod.CANCEL_PENDING_LOOP)
    async def loop_cancel(self, args: WireArgs) -> bool:
        return await self._controllers.loop.cancel_pending()

    @rpc(RpcMethod.LOOP_STATUS)
    async def loop_status(self, args: WireArgs) -> LoopStatusSnapshot:
        return await self._ctx.backend.loop_status()

    @rpc(RpcMethod.LOOP_RESUME)
    async def loop_resume(self, args: WireArgs) -> str:
        return await self._controllers.loop.resume()

    @rpc(RpcMethod.LOOP_PAUSE)
    async def loop_pause(self, args: WireArgs) -> bool:
        return await self._ctx.backend.loop_pause()

    @rpc(RpcMethod.COMPACT_IF_NEEDED, args=CompactIfNeededArgs)
    async def compact_if_needed(self, args: CompactIfNeededArgs) -> msg.SessionCleared | None:
        return await self._controllers.context.compact_if_needed(args.ctx_tokens, args.max_ctx)

    @rpc(RpcMethod.EXTRACT_LEARNINGS, args=ExtractLearningsArgs)
    async def extract_learnings(self, args: ExtractLearningsArgs) -> None:
        await self._controllers.context.extract_learnings(args.user_msg, args.assistant_msg)
