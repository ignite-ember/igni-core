"""How the pool's sessions are made: the boot one, and each one it resumes."""

from __future__ import annotations

from collections.abc import MutableSequence
from dataclasses import dataclass
from pathlib import Path

from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.rpc_handlers.base import RpcCall
from igni_core.backend.rpc_router import RpcRouter
from igni_core.backend.schemas_sessions import BackendLike
from igni_core.backend.server import BackendServer
from igni_core.backend.session_pool import SessionRuntime
from igni_core.backend.session_stamping_transport import SessionStampingTransport
from igni_core.backend.workspace_trust_controller import reload_settings
from igni_core.core.config.cli_overrides import CliOverrides
from igni_core.core.config.settings import Settings
from igni_core.core.workspace_trust import process_trust


@dataclass(frozen=True, kw_only=True)
class BootRuntime:
    """The session the backend booted with, wired before the pool exists."""

    backend: BackendLike
    rpc_table: dict[str, RpcCall]
    router: RpcRouter
    queue: MutableSequence[str]


class SessionRuntimeFactory:
    """Builds a :class:`SessionRuntime` per session, from the pool's shared wiring."""

    def __init__(
        self,
        *,
        wiring: PoolWiring,
        settings: Settings,
        dirs: PoolDirectories,
        server_cls: type[BackendServer] = BackendServer,
        cli_overrides: CliOverrides | None = None,
    ) -> None:
        self._wiring = wiring
        self._settings = settings
        self._dirs = dirs
        # What the backend's command line set. ``settings`` hold them already,
        # merged with the boot folder's files; a session in another folder
        # loads that folder's files, and these go on top again.
        self._cli_overrides = cli_overrides
        # What a resumed session runs on. A constructor argument
        # rather than a class attribute: a seam a test rebinds on the
        # class is a seam every instance of that class sees, which is
        # a global wearing a class's clothes.
        self._server_cls = server_cls
        # Whether the folder the settings were loaded for was trusted then:
        # a folder trusted (or distrusted) since brings (or drops) its own
        # ``permissions`` and ``safety``, which these settings do not hold.
        self._settings_trusted = self._trusted(dirs.project_dir)

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
        project_dir = self._dirs.dir_for(session_id)
        backend = self._server_cls(
            self._settings_for(project_dir),
            project_dir=project_dir,
            resume_session_id=session_id,
            additional_dirs=self._dirs.additional_dirs,
            contributions=self._wiring.contributions,
            prompt_loader=self._wiring.prompt_loader,
            # The extension tools' hints, shared by the whole pool: the
            # installer fills one table, and every session of the pool
            # judges those tools by it.
            tool_hints=self._wiring.tool_hints,
            # The supervisor the whole backend process shares — the
            # resumed session's processes land on the same registry
            # the watcher panel reads.
            supervisors=self._wiring.supervisors,
            # The gate of the whole backend process — a resumed session
            # asks the same gate the boot session does, because it is a
            # session of that process.
            gate=self._wiring.gate,
        )
        await backend.startup()
        if self._wiring.extension_mcp is not None:
            # The extensions run once, on the boot session's manager; this
            # session is offered their tools too, and its agents are built
            # again to carry them.
            backend.mcp_manager.share_extensions_of(self._wiring.extension_mcp)
            backend.reload_agents()
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

    def _settings_for(self, project_dir: Path) -> Settings:
        """The settings a session in ``project_dir`` starts on.

        In the boot folder, the process's, deep-copied: resuming applies the
        session's persisted model preference, which must not leak into other
        runtimes' defaults. Loaded again when the folder's trust is not what
        it was when the process's were loaded: a session made after
        ``set_workspace_trust`` ignored the folder's allow and deny rules,
        while its own ``check_permission`` already read them.

        In any other folder, that folder's own: the process's were merged
        from the boot folder's ``.igni`` files, and a window working in
        another folder ran on them — the boot folder's permissions, MCP
        servers and hooks. Loaded the way the boot folder's were, with the
        same trust gate (an untrusted folder's ``permissions`` and ``safety``
        are dropped by the project tiers) and the command line's overrides
        on top.
        """
        if not self._is_boot_folder(project_dir) or (
            self._trusted(project_dir) != self._settings_trusted
        ):
            return reload_settings(project_dir, self._cli_overrides)
        return self._settings.model_copy(deep=True)

    def _is_boot_folder(self, project_dir: Path) -> bool:
        return Path(project_dir).resolve() == Path(self._dirs.project_dir).resolve()

    @staticmethod
    def _trusted(project_dir: Path) -> bool:
        return process_trust().decide(project_dir).trusted

    @staticmethod
    def bind(
        *,
        backend: BackendLike,
        rpc_table: dict[str, RpcCall],
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
