"""One backend, many live sessions: every message a window sends, to where it belongs.

:class:`SessionOrchestrator` is the coordinator. Each message passes, in
order:

* admission (:mod:`~igni_core.backend.message_admission`): refused, in
  the shape its sender waits for, when its connection may not send it;
* the pool-level calls (:mod:`~igni_core.backend.pool_rpcs`): attaching a
  session, the host and window models, a page's service token, the
  platform's calls and the approver tokens, answered by the pool;
* the sessions (:mod:`~igni_core.backend.session_router`): everything
  else, to the runtime its ``session_id`` names.

Each runs in the background (:mod:`~igni_core.backend.in_flight_tasks`),
so a long stream does not hold up later calls, and shutdown lets them
finish. How a session is made is
:mod:`~igni_core.backend.session_runtime_factory`'s; which directory it
works in, :mod:`~igni_core.backend.pool_directories`'.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from igni_core.backend.in_flight_tasks import InFlightTasks
from igni_core.backend.message_admission import MessageAdmission
from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.pool_rpcs import AttachSessionRpc, PoolRpc, PoolRpcRouter
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.schemas_pool import AttachRefused, ExtensionPost, ProjectDirs
from igni_core.backend.schemas_rpc import AttachSessionResult, SessionPoolConfig
from igni_core.backend.session_pool import SessionPool
from igni_core.backend.session_router import InboundWatcher, SessionRouter
from igni_core.backend.session_runtime_factory import BootRuntime, SessionRuntimeFactory
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.client_state import ClientStateStore
from igni_core.core.session.session_directories import SessionDirectoryStore
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import Message

logger = logging.getLogger(__name__)


class SessionOrchestrator:
    """Admission, then the pool's own calls, then the sessions; and the serve loop."""

    def __init__(
        self,
        *,
        sessions: SessionRouter,
        pool_rpcs: PoolRpcRouter,
        admission: MessageAdmission,
        tasks: InFlightTasks,
        client_state: ClientStateStore,
    ) -> None:
        self._sessions = sessions
        self._pool_rpcs = pool_rpcs
        self._admission = admission
        self._tasks = tasks
        self._client_state = client_state

    @classmethod
    def start(
        cls,
        *,
        boot: BootRuntime,
        wiring: PoolWiring,
        settings: Settings,
        dirs: ProjectDirs,
        factory: type[SessionRuntimeFactory] = SessionRuntimeFactory,
    ) -> SessionOrchestrator:
        """The pool over the boot session, every part of it wired."""
        storage = StorageLocator.from_settings(settings)
        directories = PoolDirectories(SessionDirectoryStore.from_locator(storage), dirs)
        directories.record(boot.backend.session_id, boot.backend.project_dir)
        runtimes = factory(wiring=wiring, settings=settings, dirs=directories)
        default = runtimes.boot(boot)
        sessions = SessionRouter(
            pool=SessionPool(default, runtimes.create, config=SessionPoolConfig.from_env()),
            dirs=directories,
            wiring=wiring,
            default_dispatcher=wiring.dispatcher_for(default),
        )
        pool_rpcs = PoolRpcRouter(PoolReplier(wiring.transport))
        pool_rpcs.mount(AttachSessionRpc(sessions))
        return cls(
            sessions=sessions,
            pool_rpcs=pool_rpcs,
            admission=MessageAdmission(wiring.extensions),
            tasks=InFlightTasks(),
            # Per-client UI state: survives reloads, and is shared by every
            # client (web, desktop) holding the same opaque ``client_id``.
            client_state=ClientStateStore.from_locator(storage),
        )

    @property
    def extensions(self) -> ExtensionRpc:
        return self._admission.extensions

    @property
    def client_state(self) -> ClientStateStore:
        """The store every window's lasting model fields are kept in."""
        return self._client_state

    @property
    def pool(self) -> SessionPool:
        return self._sessions.pool

    @property
    def in_flight(self) -> InFlightTasks:
        return self._tasks

    def refresh_agents(self) -> None:
        self._sessions.refresh_agents()

    async def attach(
        self, session_id: str, project_dir: str | None
    ) -> AttachSessionResult | AttachRefused:
        """The check a window's attach passes, in one place, so that the host
        model's ``host.sessions`` is refused exactly where a window would be."""
        return await self._sessions.attach(session_id, project_dir)

    def mount(self, rpc: PoolRpc) -> None:
        """``rpc`` answers its pool-level methods from now on."""
        self._pool_rpcs.mount(rpc)

    def watch_inbound(self, watcher: InboundWatcher) -> None:
        """Be shown every message a window sends that a runtime handles."""
        self._sessions.watch_inbound(watcher)

    def spawn_dispatch(self, message: Message) -> asyncio.Task[None]:
        """Dispatch a message as a window's would be, in the background."""
        return self._tasks.spawn(self.dispatch(message))

    async def post_from_extension(self, post: ExtensionPost) -> None:
        """A message in a session's chat, labelled as an extension's.

        The agent answers it as it answers the person — the run streams to
        every window — but nothing about it says the person wrote it: every
        window labels it as the extension's, and the agent is told who sent it.
        """
        runtime = await self._sessions.pool.get_or_create(post.session_id)
        dispatcher = self._sessions.dispatcher_for(runtime)
        if post.queued:
            await dispatcher.queue_from_extension(post.extension_id, post.text, post.from_service)
            return
        self._tasks.spawn(
            dispatcher.post_from_extension(
                post.extension_id, post.text, post.media, post.from_service
            )
        )

    async def dispatch(self, message: Message) -> None:
        """Admit ``message``, then answer it at the pool level or route it to its session.

        The top-level crash boundary of a background task: whatever goes
        wrong handling one message is logged and becomes an ``Error`` for the
        windows, and the next message is handled as usual.
        """
        try:
            refusal = self._admission.refusal(message)
            if refusal is not None:
                await self._pool_rpcs.replier.refuse(message, refusal.reason)
            elif self._pool_rpcs.handles(message):
                await self._pool_rpcs.handle(message)
            else:
                await self._sessions.route(message)
        except Exception as exc:
            logger.error("message handler crashed: %s", exc, exc_info=True)
            with contextlib.suppress(Exception):
                await self._pool_rpcs.replier.broadcast(
                    msg.Error(
                        id=message.id,
                        session_id=message.session_id,
                        text=f"session routing failed: {exc}",
                    )
                )

    async def serve(self, shutdown_event: asyncio.Event) -> None:
        """Read from the windows until shutdown, each message its own task, then
        let the ones in flight finish."""
        async for message in self._pool_rpcs.replier.incoming():
            if shutdown_event.is_set():
                break
            if isinstance(message, msg.Shutdown) and self._admission.refusal(message) is None:
                # Only a connection that may stop igni stops it; another's
                # ``shutdown`` is refused like any message it may not send.
                break
            self._tasks.spawn(self.dispatch(message))
        await self._tasks.drain()
