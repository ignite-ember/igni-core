"""Per-message dispatcher.

Replaces the 11-branch ``isinstance`` chain that used to live in the
backend entry point. Each
message class now maps to a bound method on :class:`MessageDispatcher`;
:meth:`dispatch` is the single entry point the receive loop calls.

The dispatcher's collaborators are:

* the target ``backend`` (per-runtime — the pool picks it before
  dispatch),
* the target ``transport`` (a stamping wrapper for pooled runtimes,
  the raw transport for the default runtime),
* the ``rpc_table`` from :class:`RpcRouter.build_table`,
* the per-runtime queue for injected user messages,
* an optional :class:`LoginCoordinator` (only the default-runtime
  dispatcher owns login; pooled runtimes get ``None``).

What the agent is told around a message (``/plan``, an extension's
post) lives in :mod:`igni_core.backend.agent_framing`; the wire shape
of an RPC result in :mod:`igni_core.backend.rpc_result`.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import AsyncIterator, Callable, Mapping, MutableSequence
from typing import Any

from igni_core.backend.agent_framing import ExtensionMessage, PlanResearchHint
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.rpc_errors import code_of
from igni_core.backend.rpc_result import RpcResult
from igni_core.backend.schemas_history import AssistantTurn
from igni_core.backend.schemas_sessions import DispatchedBackend, TransportLike
from igni_core.core.activity import BusyKind, background
from igni_core.core.sentiment.judge import SentimentJudge
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.extensions.registry import ExtensionRefused
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import Message
from igni_core.protocol.schemas.push_payloads import SessionNamedPayload, UserSentimentPayload
from igni_core.protocol.schemas.rpc import RpcErrorCode

logger = logging.getLogger(__name__)


class _RunOutcome(enum.Enum):
    """How one streamed run finished."""

    ENDED = "ended"
    PAUSED = "paused"
    FAILED = "failed"


class MessageDispatcher:
    """Route one wire message to its per-message-class handler.

    Instances are cheap — the pool creates one per runtime so each
    dispatcher can hold direct references to its runtime's
    backend / transport / queue rather than passing them through
    every call site.
    """

    def __init__(
        self,
        *,
        backend: DispatchedBackend,
        transport: TransportLike,
        rpc_table: Mapping[str, Callable[[dict[str, Any]], Any]],
        queue: MutableSequence[str],
        login: LoginCoordinator | None,
        extensions: ExtensionRpc | None = None,
        sentiment: SentimentJudge | None = None,
    ) -> None:
        self._backend = backend
        self._transport = transport
        self._rpc_table = rpc_table
        self._queue = queue
        self._login = login
        # Absent, an ``ext.*`` call is refused with the reason rather
        # than falling through to ``Unknown RPC method``, which is
        # what it did for as long as the namespace did not exist and
        # is the least informative thing it could say.
        self._extensions = extensions
        self._sentiment = sentiment

    async def dispatch(self, message: Message) -> None:
        req_id = message.id or ""
        match message:
            case msg.UserMessage():
                await self._on_user_message(message, req_id)
            case msg.HITLResponse():
                await self._on_hitl_response(message, req_id)
            case msg.HITLResponseBatch():
                await self._on_hitl_response_batch(message, req_id)
            case msg.Command():
                await self._on_command(message, req_id)
            case msg.SessionList():
                await self._on_session_list(req_id)
            case msg.SessionSwitch():
                await self._on_session_switch(message, req_id)
            case msg.ModelSwitch():
                await self._on_model_switch(message, req_id)
            case msg.MCPToggle():
                await self._on_mcp_toggle(message, req_id)
            case msg.QueueMessage():
                await self._on_queue_message(message)
            case msg.Typing():
                await self._on_typing(message)
            case msg.Cancel():
                self._on_cancel()
            case msg.CancelLogin():
                await self._on_cancel_login()
            case msg.RPCRequest():
                await self._on_rpc_request(message, req_id)
            case _:
                logger.warning("Unknown FE message type: %s", type(message).__name__)

    # ── User-driven message paths ────────────────────────────────

    async def _on_user_message(self, message: msg.UserMessage, req_id: str) -> None:
        # Mirroring: every attached view paints the user bubble; the
        # sender recognises its own client_id and skips the echo.
        # Send the ORIGINAL (un-wrapped) text so chat bubbles stay
        # clean — the system-context wrapper below is for the LLM
        # only, not for display.
        # Typed check: a loosely held backend (a test double) answers with a truthy mock.
        if self._backend.turn_in_progress is True:
            await self._queue_typed_during_a_turn(message, req_id)
            return
        await self._transport.send(
            msg.UserMessageReceived(text=message.text, client_id=message.client_id)
        )
        run_id = self._spawn_sentiment(message.text)
        # ``force=true`` is the FE's "Retry" semantic on an
        # interrupted banner: supersede any pending + interrupted
        # rows for this session, then start clean. Without this
        # the retry would create a second pending row that dangles
        # until the new run completes (and on failure, would leave
        # BOTH the old and new rows stamped interrupted). Done
        # before ``run_message`` so the inner ``record_received``
        # call lands on a clean slate.
        if message.force:
            journal = self._backend.controllers.runs.pending_journal
            discarded = await journal.discard_all_for_session()
            logger.debug(
                "user_message(force=true) discarded %d stale row(s) for session",
                discarded,
            )
        agent_text = self._maybe_wrap_plan_hint(message.text)
        await self._run_turn(agent_text, message.file_contents, req_id, run_id)

    async def _queue_typed_during_a_turn(self, message: msg.UserMessage, req_id: str) -> None:
        """A message sent while a turn works is queued for that turn, as a ``queue_message`` is.

        The turn takes it after its next tool call; one that ends with it
        still queued runs it next (:meth:`_run_queued`). Started beside the
        turn, its run read a history without the turn's message or work —
        a turn not finished is not in it — and the two raced over the
        conversation. ``force`` is not honoured here: it discards the pending
        messages, and the working turn's is one of them. The message's own
        request ends here, with its ``StreamEnd``: its answer streams in the
        working turn's, and a window waits for that frame without a timeout.
        """
        run_id = self._spawn_sentiment(message.text)
        if run_id is not None:
            run_id.set_result(None)
        await self._on_queue_message(
            msg.QueueMessage(text=message.text, client_id=message.client_id)
        )
        await self._transport.send(msg.StreamEnd(id=req_id))

    async def _run_turn(
        self,
        agent_text: str,
        files: dict[str, str],
        req_id: str,
        run_id: asyncio.Future[str | None] | None = None,
    ) -> None:
        """Run one turn and stream it to every view.

        ``files`` is a path → content map of *text* files: the wire's
        ``file_contents`` for a person's message, the host API's
        ``attachments`` for an extension's. Agno's ``arun`` takes live
        ``Image`` / ``Audio`` / ``Video`` / ``File`` containers, so a
        map of text files was never attachable — the old code turned
        it into extra fields on the media bundle and handed them to
        ``arun`` as unknown keyword arguments, which Agno dropped.
        The model's own tools read such files instead, under their
        permissions. ``run_id`` is told the id of the run, once it is known.
        """
        if files:
            logger.debug("message carries %d file(s) of text", len(files))
        outcome = await self._stream(
            self._backend.run_message(agent_text, media=None), req_id, run_id
        )
        if outcome is _RunOutcome.FAILED:
            return

        # First completed run: name the session in the background so
        # a queued follow-up isn't blocked on the naming model call.
        self._spawn_auto_name()
        if outcome is _RunOutcome.ENDED:
            await self._run_queued()

    async def _run_queued(self) -> None:
        """Run, in order, what was queued for after the run and is still waiting.

        A tool call inside the run takes a queued message into that run
        (``core/hooks/queue``); a run with no tool call left it waiting until
        some later, unrelated run called a tool. A message still queued when
        the run ends is the next turn, as ``session.queue()`` promises. It
        leaves the queue as its run starts: the run's own pending-message
        record is what survives a crash from then on.

        A run that paused for a person's approval has not ended: the queue
        waits for the resumed run (:meth:`_on_hitl_response`), whose
        approved tool call takes it in. Draining at the pause started a
        second run beside the paused one, with no view of the turn.
        """
        while len(self._queue):
            text = self._queue[0]
            del self._queue[0]
            outcome = await self._stream(self._backend.run_message(text, media=None), "")
            if outcome is not _RunOutcome.ENDED:
                return

    async def _stream(
        self,
        frames: AsyncIterator[Any],
        req_id: str,
        run_id: asyncio.Future[str | None] | None = None,
    ) -> _RunOutcome:
        """Send ``frames`` to every view under ``req_id``, then ``StreamEnd``.

        ``StreamEnd`` is what a window waits for to finish the reply, and it
        waits without a timeout: a run that raised midway used to skip it,
        so the window spun forever while the error went out under the
        request's id after the fact. A failure is now an ``Error`` inside
        the stream, and the stream still ends. Says whether the run failed, paused for a
        person, or ended. ``run_id`` gets the top-level run's id, or None when none started.
        """
        outcome = _RunOutcome.ENDED
        try:
            async for proto in frames:
                if (
                    run_id is not None
                    and not run_id.done()
                    and isinstance(proto, msg.RunStarted | msg.RunCompleted)
                    and proto.run_id
                    and not proto.parent_run_id
                ):
                    run_id.set_result(proto.run_id)
                if isinstance(proto, msg.RunPaused):
                    outcome = _RunOutcome.PAUSED
                elif isinstance(proto, msg.RunCompleted):
                    outcome = _RunOutcome.ENDED
                if req_id:
                    proto = proto.model_copy(update={"id": req_id})
                await self._transport.send(proto)
        except Exception as exc:
            logger.error("the run failed mid-stream: %s", exc, exc_info=True)
            await self._transport.send(msg.Error(id=req_id, text=f"The run failed: {exc}"))
            return _RunOutcome.FAILED
        finally:
            if run_id is not None and not run_id.done():
                run_id.set_result(None)
            await self._transport.send(msg.StreamEnd(id=req_id))
        return outcome

    # ── An extension's messages ──────────────────────────────────

    async def post_from_extension(
        self, extension_id: str, text: str, media: ChatAttachments, from_service: str = ""
    ) -> None:
        """``host.chat.post``: a message from an extension, for the agent.

        Every view paints it labelled as the extension's — never as the
        person's, which an extension cannot post as — and the agent is
        told who sent it, then answers in the chat as it answers the
        person. ``from_service`` is the client id of the extension's
        service when that service sent it (``docs/CLOUD.md`` §6.1).
        """
        await self._transport.send(
            msg.UserMessageReceived(
                text=text,
                client_id="",
                extension_id=extension_id,
                service_client_id=from_service,
            )
        )
        await self._run_turn(ExtensionMessage.wrap(extension_id, text), media.files, "")

    async def queue_from_extension(
        self, extension_id: str, text: str, from_service: str = ""
    ) -> None:
        """``host.chat.queue``: the same, for the next turn."""
        self._queue.append(ExtensionMessage.wrap(extension_id, text))
        await self._transport.send(
            msg.UserMessageReceived(
                text=text,
                client_id="",
                queued=True,
                extension_id=extension_id,
                service_client_id=from_service,
            )
        )

    def _maybe_wrap_plan_hint(self, text: str) -> str:
        """Prefix the one-shot ``/plan`` nudge when ``/plan`` armed it."""
        if not self._backend.consume_plan_research_flag():
            return text
        return PlanResearchHint.wrap(text)

    def _spawn_auto_name(self) -> None:
        """Fire-and-forget: name the session after the first
        completed run. The task handle is parked on the
        :class:`SessionRuntime` so the loop doesn't GC it mid-flight.

        Falls back to an orphaned task when no runtime is attached
        (test harnesses that construct dispatchers without booting
        an orchestrator) — the coroutine finishes quickly and the
        transport.send is idempotent-safe.
        """
        coro = self._auto_name_coro()
        runtime = self._backend.runtime
        if runtime is not None:
            runtime.spawn_auto_name(coro)
        else:
            background.start(
                coro, holder="auto-name", reason="naming the session", kind=BusyKind.POST_RUN
            )

    async def _auto_name_coro(self) -> None:
        # ``maybe_auto_name_session`` now returns an ``AutoNameResult``
        # envelope; the FE push fires only when a fresh name was
        # generated (``ok=True``), matching the pre-refactor
        # truthy-string check byte-for-byte.
        result = await self._backend.maybe_auto_name_session()
        if not result.ok or not result.name:
            return
        await self._transport.send(
            msg.push_session_named(
                SessionNamedPayload(session_id=self._backend.session_id, name=result.name)
            )
        )

    def _spawn_sentiment(self, text: str) -> asyncio.Future[str | None] | None:
        """Beside the run, never before it: ask Laya how ``text`` feels about igni.

        Returns where the run is to tell its id, when Laya is asked.
        """
        judge = self._sentiment
        if judge is None or not judge.worth_asking(text):
            return None
        run_id: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        background.start(
            self._sentiment_coro(judge, self._backend.session_id, text, run_id),
            holder="sentiment",
            reason="reading how a message feels about igni",
        )
        return run_id

    async def _sentiment_coro(
        self,
        judge: SentimentJudge,
        session_id: str,
        text: str,
        run_id: asyncio.Future[str | None],
    ) -> None:
        verdict = await judge.judge(text)
        if verdict is None:
            return
        message_run_id = await run_id
        await self._transport.send(
            msg.push_user_sentiment(
                UserSentimentPayload(
                    session_id=session_id,
                    sentiment=verdict.sentiment,
                    confidence=verdict.confidence,
                    message_run_id=message_run_id,
                    reply_run_id=await self._reply_before(session_id, message_run_id),
                    aspects=verdict.aspects,
                )
            )
        )

    async def _reply_before(self, session_id: str, message_run_id: str | None) -> str | None:
        """The run of the last assistant reply before the message's run; None when there is none."""
        try:
            turns = await self._backend.get_chat_history(session_id)
        except Exception as exc:  # noqa: BLE001 — a reference is optional; the push still goes out
            logger.debug("sentiment: the history could not be read (%s)", exc)
            return None
        mine = [
            i for i, turn in enumerate(turns) if message_run_id and turn.run_id == message_run_id
        ]
        before = turns[: mine[0]] if mine else turns
        for turn in reversed(before):
            if isinstance(turn, AssistantTurn) and turn.run_id:
                return turn.run_id
        return None

    async def _on_hitl_response(self, message: msg.HITLResponse, req_id: str) -> None:
        # Mirroring: dismiss the now-stale permission dialog on
        # every other view before the resumed run starts streaming.
        await self._transport.send(msg.RequirementResolved(requirement_id=message.requirement_id))
        outcome = await self._stream(
            self._backend.resolve_hitl(message.requirement_id, message.action, message.choice),
            req_id,
        )
        if outcome is _RunOutcome.ENDED:
            await self._run_queued()

    async def _on_hitl_response_batch(self, message: msg.HITLResponseBatch, req_id: str) -> None:
        for decision in message.decisions:
            await self._transport.send(
                msg.RequirementResolved(requirement_id=decision.requirement_id)
            )
        outcome = await self._stream(self._backend.resolve_hitl_batch(message.decisions), req_id)
        if outcome is _RunOutcome.ENDED:
            await self._run_queued()

    async def _on_command(self, message: msg.Command, req_id: str) -> None:
        result = await self._backend.handle_command(message.text)
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_session_list(self, req_id: str) -> None:
        result = await self._backend.list_sessions()
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_session_switch(self, message: msg.SessionSwitch, req_id: str) -> None:
        result = await self._backend.switch_session(message.session_id)
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_model_switch(self, message: msg.ModelSwitch, req_id: str) -> None:
        result = self._backend.switch_model(message.model_name)
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_mcp_toggle(self, message: msg.MCPToggle, req_id: str) -> None:
        result = await self._backend.toggle_mcp(message.server_name, message.connect)
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_queue_message(self, message: msg.QueueMessage) -> None:
        self._queue.append(message.text)
        await self._transport.send(
            msg.UserMessageReceived(text=message.text, client_id=message.client_id, queued=True)
        )

    async def _on_typing(self, message: msg.Typing) -> None:
        """Pure fan-out: the BE adds nothing. Relayed to ALL views
        (sender included — it filters by ``client_id``)."""
        await self._transport.send(message)

    def _on_cancel(self) -> None:
        self._backend.cancel_run()

    async def _on_cancel_login(self) -> None:
        if self._login is not None:
            await self._login.cancel()

    async def _on_rpc_request(self, message: msg.RPCRequest, req_id: str) -> None:
        # The extension namespace is checked before the base table,
        # not after. A base method could otherwise shadow an
        # extension's by being named ``ext.something`` — unlikely, but
        # the failure would be an extension silently served by igni's
        # own handler, which is the worst shape a routing bug can
        # take.
        if ExtensionRpc.owns(message.method):
            await self._on_extension_rpc(message, req_id)
            return

        handler = self._rpc_table.get(message.method)
        if handler is None:
            await self._reply(
                message,
                msg.RPCResponse.fail(
                    req_id, f"Unknown RPC method: {message.method}", RpcErrorCode.UNKNOWN_METHOD
                ),
            )
            return
        try:
            result = handler(message.args)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result
            if isinstance(result, Message):
                result = result.model_copy(update={"id": req_id})
                await self._reply(message, result)
            else:
                await self._reply(message, msg.RPCResponse.ok(req_id, RpcResult.serialize(result)))
        except KeyError as exc:
            # A handler reading `args["session_id"]` on a call that
            # omitted it raises `KeyError('session_id')`, and
            # `str(exc)` is the bare `"'session_id'"`. That reached the
            # client as the entire error message: no method, no
            # indication it was about an argument at all. Sweeping the
            # API, `check_permission` gave up its three required keys
            # one refusal at a time — `tool_args`, then `tool_name`,
            # then `func_name` — with nothing to say they were
            # arguments rather than a fault.
            #
            # Two handlers already did this properly
            # (`run_workflow: 'name' is required`), so the wording is
            # theirs; this makes it the default instead of the
            # exception. Narrowed to `KeyError` on purpose: a
            # `KeyError` raised *inside* real logic is now slightly
            # mislabelled, which is the cost of not having to annotate
            # every handler, and it is a better trade than the caller
            # being told only `'url'`.
            logger.error("RPC %s failed: missing argument %s", message.method, exc)
            await self._reply(
                message,
                msg.RPCResponse.fail(
                    req_id, f"{message.method}: {exc} is a required argument", RpcErrorCode.INVALID
                ),
            )
        except Exception as exc:
            logger.error("RPC %s failed: %s", message.method, exc, exc_info=True)
            await self._reply(message, msg.RPCResponse.fail(req_id, str(exc), code_of(exc)))

    async def _on_extension_rpc(self, message: msg.RPCRequest, req_id: str) -> None:
        """Serve one ``ext.<id>.<method>`` call.

        Separate from :meth:`_on_rpc_request` because the failure
        modes have nothing in common. A base RPC fails by raising out
        of a handler; an extension call fails by being refused, and a
        refusal is a sentence written for the extension's author with
        the manifest line that would fix it. Folding the two together
        would mean wrapping refusals in the "is a required argument"
        machinery above, which is about something else entirely.
        """
        if self._extensions is None:
            await self._reply(
                message,
                msg.RPCResponse.fail(
                    req_id,
                    f"{message.method}: this backend has no extension support, "
                    "so nothing can answer the 'ext.' namespace.",
                    RpcErrorCode.UNAVAILABLE,
                ),
            )
            return
        try:
            result = await self._extensions.dispatch(
                caller=message.caller,
                method=message.method,
                args=message.args,
            )
        except ExtensionRefused as exc:
            # Expected, and not worth a traceback: a refusal is the
            # system working. Logged at info so an administrator
            # chasing "why can it not call this" has a record.
            logger.info("extension rpc %s refused: %s", message.method, exc)
            await self._reply(
                message, msg.RPCResponse.fail(req_id, str(exc), RpcErrorCode.FORBIDDEN)
            )
            return
        except Exception as exc:
            logger.error("extension rpc %s failed: %s", message.method, exc, exc_info=True)
            await self._reply(message, msg.RPCResponse.fail(req_id, str(exc), code_of(exc)))
            return
        await self._reply(message, msg.RPCResponse.ok(req_id, RpcResult.serialize(result)))

    async def _reply(self, request: msg.RPCRequest, response: Message) -> None:
        """Answer a call on the connection that made it, and no other.

        An answer is the caller's: a file's contents, a shell command's
        output, an extension's data, a status. Broadcast, every client
        attached — another window, a program with the lockfile's token —
        read it too. A request from a transport without connections (the
        terminal's) has no ``from_client``, and its answer goes out as
        before.
        """
        response.address_to(request.from_client)
        await self._transport.send(response)
