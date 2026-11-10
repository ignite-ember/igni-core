"""A message for a session, to that session's runtime.

Everything a window sends that is not a pool-level call routes by its
``session_id`` stamp: an empty one to the boot session, an id the pool
has not loaded to that session resumed. The session list is the boot
project's whatever the stamp says.
"""

from __future__ import annotations

from typing import Protocol

from igni_core.backend.message_dispatcher import MessageDispatcher
from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.schemas_pool import AttachRefused
from igni_core.backend.schemas_rpc import AttachSessionResult
from igni_core.backend.session_pool import SessionPool, SessionRuntime
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import Message


class InboundWatcher(Protocol):
    """Shown every message a window sends that a runtime handles."""

    def __call__(self, message: Message, /) -> None: ...


class SessionRouter:
    """The pool, and the routing of each message to one of its runtimes."""

    def __init__(
        self,
        *,
        pool: SessionPool,
        dirs: PoolDirectories,
        wiring: PoolWiring,
        default_dispatcher: MessageDispatcher,
    ) -> None:
        self._pool = pool
        self._dirs = dirs
        self._wiring = wiring
        self._default_dispatcher = default_dispatcher
        self._watchers: list[InboundWatcher] = []

    @property
    def pool(self) -> SessionPool:
        return self._pool

    def watch_inbound(self, watcher: InboundWatcher) -> None:
        self._watchers.append(watcher)

    async def attach(
        self, session_id: str, project_dir: str | None
    ) -> AttachSessionResult | AttachRefused:
        """Bind or create a session, optionally in a specific directory.

        Attaching is not looking: a window says which session it shows by
        writing its model's ``session``, so nothing is announced here.
        """
        claimed = self._dirs.claim(session_id, project_dir)
        if isinstance(claimed, AttachRefused):
            return claimed
        runtime = await self._pool.get_or_create(claimed)
        return AttachSessionResult(
            session_id=runtime.backend.session_id,
            project_dir=str(runtime.backend.project_dir),
        )

    def dispatcher_for(self, runtime: SessionRuntime) -> MessageDispatcher:
        if runtime is self._pool.default:
            return self._default_dispatcher
        return self._wiring.dispatcher_for(runtime)

    async def route(self, message: Message) -> None:
        for watcher in self._watchers:
            watcher(message)
        if isinstance(message, msg.SessionList):
            await self._default_dispatcher.dispatch(message)
            return
        runtime = await self._pool.get_or_create(message.session_id)
        # Before and after: handling it can change the runtime's id (``/clear``,
        # a resume), and the pool routes the old and the new id by ``known_ids``.
        runtime.register_id()
        await self.dispatcher_for(runtime).dispatch(message)
        runtime.register_id()
        self._dirs.record_if_changed(runtime)

    def refresh_agents(self) -> None:
        """Rebuild every live session's agents.

        What extensions do to the prompts and agents changed; a session
        built before it would otherwise keep the old ones until something
        else rebuilt it.
        """
        for runtime in self._pool.runtimes:
            runtime.backend.reload_agents()
