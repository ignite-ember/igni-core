"""igni's backend, as the host object model reaches it.

:class:`OrchestratorHostBackend` implements
:class:`~igni_core.extensions.host.backend.HostBackend` over the session
pool: an RPC is the handler the window would have reached, an action is
dispatched as the window's would be, an attach is the window's attach —
refused where the window's is. Everything it sends goes out through the
:class:`~igni_core.backend.observed_transport.ObservedTransport`.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from pathlib import Path
from typing import Any

from igni_core.backend.activity import Activity
from igni_core.backend.observed_transport import ObservedTransport
from igni_core.backend.rpc_result import RpcResult
from igni_core.backend.rpc_router import POOL_LEVEL_RPCS
from igni_core.backend.schemas_pool import AttachRefused, ExtensionPost
from igni_core.backend.service_event_delivery import PoolSession
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.core.activity import ActivityRegistry, background
from igni_core.core.session.client_state import ClientStateStore
from igni_core.core.session.schemas import PersistResult
from igni_core.extensions.host.broadcasts.views import ViewShown
from igni_core.extensions.host.values import SessionAttached
from igni_core.extensions.service_events import UnattendedPost
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod

#: Ids the host gives actions whose answer it takes back.
HOST_ID_PREFIX = "host-"


class OrchestratorHostBackend:
    """:class:`~igni_core.extensions.host.backend.HostBackend` over the pool."""

    def __init__(
        self,
        *,
        orchestrator: SessionOrchestrator,
        transport: ObservedTransport,
        project_dir: Path,
        client_state: ClientStateStore,
        activities: ActivityRegistry,
    ) -> None:
        self._orchestrator = orchestrator
        self._transport = transport
        self.project_dir = project_dir
        self.client_state = client_state
        self._activities = activities
        self._tasks: set[asyncio.Task[None]] = set()

    def default_session(self) -> str:
        return str(self._orchestrator.pool.default.backend.session_id)

    def live_sessions(self) -> list[str]:
        return [str(rt.backend.session_id) for rt in self._orchestrator.pool.runtimes]

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        if method in POOL_LEVEL_RPCS:
            raise ValueError(f"{method.value} is answered by the host model, not called through it")
        runtime = await self._orchestrator.pool.get_or_create(session_id)
        handler = runtime.rpc_table[method]
        result = handler(dict(args))
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            result = await result
        return RpcResult.serialize(result)

    async def act(self, message: msg.Message, *, session_id: str) -> None:
        self._orchestrator.spawn_dispatch(message.model_copy(update={"session_id": session_id}))

    async def ask(
        self, message: msg.Message, *, session_id: str, timeout: float = 120.0
    ) -> msg.Message:
        message_id = f"{HOST_ID_PREFIX}{uuid.uuid4().hex}"
        answer = self._transport.expect(message_id)
        self._orchestrator.spawn_dispatch(
            message.model_copy(update={"id": message_id, "session_id": session_id})
        )
        try:
            return await asyncio.wait_for(answer, timeout)
        finally:
            self._transport.forget(message_id)

    async def attach(self, session_id: str, project_dir: str | None) -> SessionAttached:
        """The window's attach; refused as :class:`HostBackend` says, with ``ValueError``."""
        result = await self._orchestrator.attach(session_id, project_dir)
        if isinstance(result, AttachRefused):
            raise ValueError(result.reason)
        return SessionAttached(session_id=result.session_id, project_dir=result.project_dir)

    async def post(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: dict[str, str] | None,
        queued: bool,
    ) -> None:
        await self._orchestrator.post_from_extension(
            ExtensionPost(
                session_id=session_id,
                extension_id=extension_id,
                text=text,
                media=media,
                queued=queued,
            )
        )

    async def deliver(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: dict[str, str] | None,
    ) -> UnattendedPost:
        return await PoolSession(self._orchestrator, session_id).deliver(
            extension_id=extension_id, text=text, media=media
        )

    async def title(self, *, session_id: str, title: str) -> PersistResult:
        return await PoolSession(self._orchestrator, session_id).title(title)

    async def show_view(self, *, session_id: str, payload: dict[str, Any], run_id: str) -> None:
        runtime = await self._orchestrator.pool.get_or_create(session_id)
        await runtime.transport.send(
            msg.PushNotification(channel="orchestrate_event", payload=payload)
        )
        await runtime.backend.record_event("visualization_delta", payload, run_id)

    def views(self, session_id: str) -> list[ViewShown]:
        runtime = self._orchestrator.pool.find(session_id)
        if runtime is None:
            return []
        latest: dict[str, ViewShown] = {}
        for event in runtime.backend.session_events("visualization_delta"):
            payload = dict(event.payload or {})
            if not payload.get("final", True) or not payload.get("spec_id"):
                continue
            latest[str(payload["spec_id"])] = ViewShown.from_payload(session_id, payload)
        return list(latest.values())

    def activity(self) -> Activity:
        sessions = [runtime.backend.activity() for runtime in self._orchestrator.pool.runtimes]
        return Activity.now(sessions, self._activities)

    def waiting_approvals(self, session_id: str) -> list[msg.Message]:
        runtime = self._orchestrator.pool.find(session_id)
        if runtime is None:
            return []
        return list(runtime.backend.controllers.hitl.waiting_messages())

    def project_dir_of(self, session_id: str) -> Path:
        runtime = self._orchestrator.pool.find(session_id)
        if runtime is None:
            return self.project_dir
        return Path(runtime.backend.project_dir)

    def broadcast(self, message: msg.Message) -> None:
        with contextlib.suppress(RuntimeError):
            # A message to the windows: too short to count as busy.
            task = background.daemon(self._transport.send(message), name="host-broadcast")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


__all__ = ["HOST_ID_PREFIX", "OrchestratorHostBackend"]
