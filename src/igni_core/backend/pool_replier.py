"""The windows' connection, as the session pool speaks on it.

Every pool-level answer and refusal goes out through :class:`PoolReplier`,
addressed to the connection the request came on and to no other: an
answer can carry a secret (a page's service token) or one extension's
view of the model, and broadcast, every client attached would read it. A
request from a transport without connections (the terminal's) has no
``from_client``, and its answer is broadcast.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from igni_core.backend.schemas_pool import PoolAnswer
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import Message
from igni_core.protocol.schemas.enums import CommandResultKind


class WindowsTransport(Protocol):
    """What the pool needs of the root transport: send to the windows, hear them."""

    async def send(self, message: Message) -> None: ...

    def receive(self) -> AsyncIterator[Message]: ...


class PoolReplier:
    """Answers, refusals and the crash notice, in the shape each sender waits for."""

    def __init__(self, transport: WindowsTransport) -> None:
        self._transport = transport

    def incoming(self) -> AsyncIterator[Message]:
        """What the windows send, for the serve loop."""
        return self._transport.receive()

    async def reply(self, request: Message, answer: PoolAnswer) -> None:
        """``answer`` as the response to ``request``: the one place one is built."""
        if answer.error:
            response = msg.RPCResponse.fail(request.id, answer.error)
        else:
            response = msg.RPCResponse.ok(request.id, answer.wire_result())
        await self._answer(request, response)

    async def refuse(self, message: Message, reason: str) -> None:
        """Tell the connection that sent ``message`` why it was not done.

        An RPC gets its error, a slash command an error result, and an
        approval an ``Error`` and the end of the stream it would have
        resumed, so a client following that stream stops waiting.
        """
        if isinstance(message, msg.RPCRequest):
            await self.reply(message, PoolAnswer.failed(reason))
            return
        if isinstance(message, msg.Command):
            await self._answer(
                message,
                msg.CommandResult(id=message.id, kind=CommandResultKind.ERROR, content=reason),
            )
            return
        await self._answer(
            message, msg.Error(id=message.id, session_id=message.session_id, text=reason)
        )
        await self._answer(message, msg.StreamEnd(id=message.id, session_id=message.session_id))

    async def broadcast(self, message: Message) -> None:
        """To every window: what no single request is owed."""
        await self._transport.send(message)

    async def _answer(self, request: Message, response: Message) -> None:
        await self._transport.send(response.address_to(request.from_client))
