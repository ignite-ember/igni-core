"""How the pool's sessions are made: the boot one, and each one it resumes."""

from __future__ import annotations

from collections.abc import MutableSequence
from dataclasses import dataclass
from typing import Any, ClassVar

from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.rpc_router import RpcRouter
from igni_core.backend.server import BackendServer
from igni_core.backend.session_pool import SessionRuntime
from igni_core.backend.session_stamping_transport import SessionStampingTransport
from igni_core.core.config.settings import Settings


@dataclass(frozen=True, kw_only=True)
class BootRuntime:
    """The session the backend booted with, wired before the pool exists."""

    backend: Any
    rpc_table: dict[str, Any]
    router: RpcRouter
    queue: MutableSequence[str]


class SessionRuntimeFactory:
    """Builds a :class:`SessionRuntime` per session, from the pool's shared wiring."""

    #: What a resumed session runs on. A class seam, so a test can make
    #: sessions that boot no model team.
    server_cls: ClassVar[type[BackendServer]] = BackendServer

    def __init__(self, *, wiring: PoolWiring, settings: Settings, dirs: PoolDirectories) -> None:
        self._wiring = wiring
        self._settings = settings
        self._dirs = dirs

    def boot(self, boot: BootRuntime) -> SessionRuntime:
        """The boot session as the pool's default, on a transport stamping its id."""
        return self.bind(
            backend=boot.backend,
            rpc_table=boot.rpc_table,
            queue=boot.queue,
            transport=self._wiring.stamped(boot.backend),
            router=boot.router,
        )

    async def create(self, session_id: str) -> SessionRuntime:
        """Resume ``session_id`` in its own directory, with its own router and queue."""
        # Deep-copied: resuming applies the session's persisted model
        # preference, which must not leak into other runtimes' defaults.
        backend = self.server_cls(
            self._settings.model_copy(deep=True),
            project_dir=self._dirs.dir_for(session_id),
            resume_session_id=session_id,
            additional_dirs=self._dirs.additional_dirs,
            contributions=self._wiring.contributions,
            prompt_loader=self._wiring.prompt_loader,
            # The supervisor the whole backend process shares — the
            # resumed session's processes land on the same registry
            # the watcher panel reads.
            supervisors=self._wiring.supervisors,
        )
        await backend.startup()
        queue = backend.message_queue
        backend.wire_queue_hook(queue)
        stamped = self._wiring.stamped(backend)
        # The runtime's own queue: anything the bridge appends is injected
        # as a synthetic user message in the session it belongs to.
        rt_bridge = self._wiring.push_bridge.for_transport(stamped, queue=queue)
        router = self._wiring.router_for(backend, rt_bridge)
        rpc_table = router.build_table()
        self._dirs.record(backend.session_id, backend.project_dir)
        # The session's broadcasts (plan_submitted, permission_mode_changed,
        # ...) go out stamped, so they reach the windows showing it.
        rt_bridge.bind_to_broadcast_bus(backend)
        return self.bind(
            backend=backend, rpc_table=rpc_table, queue=queue, transport=stamped, router=router
        )

    @staticmethod
    def bind(
        *,
        backend: Any,
        rpc_table: dict[str, Any],
        queue: MutableSequence[str],
        transport: SessionStampingTransport,
        router: RpcRouter,
    ) -> SessionRuntime:
        """A runtime over ``backend``, which the backend is told about."""
        runtime = SessionRuntime(
            backend=backend, rpc_table=rpc_table, queue=queue, transport=transport, router=router
        )
        backend.attach_runtime(runtime)
        return runtime
