"""``/loop`` + compaction + learning RPC handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_context import CompactionOutcome
from igni_core.backend.schemas_loop import LoopStatusSnapshot
from igni_core.core.session.schemas import LoopAdvance
from igni_core.protocol.rpc import RpcMethod, RpcScope


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

    @rpc(RpcMethod.POP_PENDING_LOOP_ITERATION, args=NoArgs, scope=RpcScope.SESSION)
    async def loop_pop(self, args: NoArgs) -> LoopAdvance | None:
        """Take the next step of this session's ``/loop``, for the client to send.

        Takes no arguments. Returns ``null`` when there is no loop or it is
        paused. Otherwise ``kind`` says what happened: ``step`` carries the
        ``prompt`` to send as the next message (and ``display_prompt`` to
        show), the iteration number and how many remain; ``completed``
        says the loop reached its cap and ended; ``safety_paused`` says it
        reached the hard cap and paused.
        """
        return await self._controllers.loop.pop_pending_iteration()

    @rpc(RpcMethod.CANCEL_PENDING_LOOP, args=NoArgs, scope=RpcScope.SESSION)
    async def loop_cancel(self, args: NoArgs) -> bool:
        """End this session's running ``/loop``.

        Takes no arguments. Returns ``true`` when a loop was ended;
        ``false`` when there was none or it is paused (a paused loop is
        resumed or left alone, not cancelled).
        """
        return await self._controllers.loop.cancel_pending()

    @rpc(RpcMethod.LOOP_STATUS, args=NoArgs, scope=RpcScope.SESSION)
    async def loop_status(self, args: NoArgs) -> LoopStatusSnapshot:
        """Read the state of this session's ``/loop``.

        Takes no arguments. Returns whether a loop is ``active`` and
        ``paused``, its prompt, which iteration it is on and how many
        remain, whether its cap was set explicitly, and the total the agent
        announced, if any.
        """
        return await self._ctx.backend.loop_status()

    @rpc(RpcMethod.LOOP_RESUME, args=NoArgs, scope=RpcScope.SESSION)
    async def loop_resume(self, args: NoArgs) -> str:
        """Resume this session's paused ``/loop``.

        Takes no arguments. Returns the prompt of the iteration to run next,
        for the client to send as a message — nothing runs on its own — or
        ``""`` when there is no paused loop.
        """
        return await self._controllers.loop.resume()

    @rpc(RpcMethod.LOOP_PAUSE, args=NoArgs, scope=RpcScope.SESSION)
    async def loop_pause(self, args: NoArgs) -> bool:
        """Pause this session's ``/loop`` after the current iteration.

        Takes no arguments. Returns ``true`` when the loop is now paused
        (or already was), ``false`` when there is no loop.
        """
        return await self._ctx.backend.loop_pause()

    @rpc(RpcMethod.COMPACT_IF_NEEDED, args=CompactIfNeededArgs, scope=RpcScope.SESSION)
    async def compact_if_needed(self, args: CompactIfNeededArgs) -> CompactionOutcome:
        """Compact this session's conversation when its context is nearly full.

        ``ctx_tokens`` is the context the last turn used and ``max_ctx``
        the model's window; at 80% or more the conversation is summarised
        and replaced by the summary (the ``PreCompact`` hooks may refuse).
        Returns ``compacted`` and, when it was, the ``summary``.
        """
        cleared = await self._controllers.context.compact_if_needed(args.ctx_tokens, args.max_ctx)
        if cleared is None:
            return CompactionOutcome(compacted=False)
        return CompactionOutcome(
            compacted=True, new_session_id=cleared.new_session_id, summary=cleared.summary
        )

    @rpc(RpcMethod.EXTRACT_LEARNINGS, args=ExtractLearningsArgs, scope=RpcScope.SESSION)
    async def extract_learnings(self, args: ExtractLearningsArgs) -> None:
        """Let igni learn from one finished turn, in the background.

        ``user_msg`` and ``assistant_msg`` are the turn's message and
        answer. Returns at once with nothing; what is learnt is kept for
        later sessions, and a failure is only logged.
        """
        await self._controllers.context.extract_learnings(args.user_msg, args.assistant_msg)
