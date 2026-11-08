"""RPC dispatch surface for the backend.

:class:`RpcRouter` is the composer that turns the per-domain
:class:`RpcHandler` subclasses into the flat
``dict[str, RpcCall]`` the receive loop calls through.
Every handler subclass owns one domain (MCP, plan mode, plugins,
etc.); this class' only job is to construct them, merge their
submaps, and validate exhaustiveness against
:class:`~igni_core.protocol.rpc.RpcMethod`.

Related-service classes (folder picker, dir scanner, shell runner,
file-completion service) live in sibling modules — see
:mod:`igni_core.backend.folder_picker`,
:mod:`igni_core.backend.dir_scanner`,
:mod:`igni_core.backend.captured_shell_runner`,
:mod:`igni_core.backend.file_completion_service`.
"""

from __future__ import annotations

import logging

from igni_core.backend.captured_shell_runner import CapturedShellRunner
from igni_core.backend.file_completion_service import FileCompletionService

# Re-exports so existing callers that reach into rpc_router for the
# picker or the pool-level constants don't need to update imports.
from igni_core.backend.folder_picker import NativeFolderPicker  # noqa: F401
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.rpc_handlers import (
    POOL_LEVEL_RPCS,
    AuthRpcHandler,
    ExtensionsRpcHandler,
    HooksRpcHandler,
    LoopRpcHandler,
    McpRpcHandler,
    ModelsRpcHandler,
    NotificationsRpcHandler,
    PanelRpcHandler,
    PlanRpcHandler,
    PluginsRpcHandler,
    PoolGuardRpcHandler,
    RpcHandler,
    RpcHandlerContext,
    SchedulerRpcHandler,
    SessionHistoryRpcHandler,
    SessionRpcHandler,
    SkillsRpcHandler,
    WorkflowsRpcHandler,
    WorkspaceTrustRpcHandler,
)
from igni_core.backend.rpc_handlers.base import RpcCall
from igni_core.backend.server import BackendServer
from igni_core.backend.workflow_runner import WorkflowRunner
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.center import NotificationCenter
from igni_core.protocol.rpc import RpcMethod, validate_rpc_table

logger = logging.getLogger(__name__)

# Re-exported at module level so the pool's admission and RPC router and any
# other module that needs the "which RPCs must be intercepted before
# per-runtime dispatch" invariant reads it from one place.
__all__ = ["POOL_LEVEL_RPCS", "NativeFolderPicker", "RpcRouter"]


class RpcRouter:
    """Compose per-domain :class:`RpcHandler` subclasses into the
    flat dispatch table the receive loop calls through.

    Handler instances are constructed once at :meth:`__init__` with a
    shared :class:`RpcHandlerContext` bundle; :meth:`build_table`
    walks the handler list and merges each one's
    :meth:`RpcHandler.methods` submap into a single ``dict`` keyed by
    the enum's wire-string value. :func:`validate_rpc_table` then
    checks the keyset covers every :class:`RpcMethod` entry so an
    added-enum-but-forgot-to-register mistake surfaces at boot.
    """

    #: Which RPCs :class:`~igni_core.backend.pool_rpcs.PoolRpcRouter`
    #: answers before per-runtime dispatch. Registered on the table as a
    #: shared "you should never reach here" guard so a regression in
    #: the intercept path fails loud instead of silently mis-routing.
    POOL_LEVEL_RPCS: frozenset[RpcMethod] = POOL_LEVEL_RPCS

    def __init__(
        self,
        *,
        backend: BackendServer,
        login: LoginCoordinator,
        push: PushNotificationBridge,
        extensions: ExtensionRpc | None = None,
        origins: ExtensionOrigins | None = None,
        extension_store: ExtensionStore | None = None,
        notifications: NotificationCenter | None = None,
    ) -> None:
        # Shared services the panel-cluster handlers depend on.
        # Cached here so per-runtime routers each get their own
        # warm ``FileIndex`` instead of sharing one across sessions
        # (a stale index would list files from the wrong project).
        file_completion = FileCompletionService(project_dir=backend.project_dir)
        shell_runner = CapturedShellRunner(project_dir=backend.project_dir)
        self._workflows = WorkflowRunner.for_project(project_dir=backend.project_dir, push=push)

        ctx = RpcHandlerContext(
            backend=backend,
            login=login,
            push=push,
            file_completion=file_completion,
            shell_runner=shell_runner,
            workflows=self._workflows,
            extensions=extensions,
            origins=origins,
            extension_store=extension_store,
            notifications=notifications,
        )
        # Order is descriptive, not load-bearing — the merge below
        # raises on duplicate keys so mis-registration surfaces
        # immediately.
        self._handlers: list[RpcHandler] = [
            McpRpcHandler(ctx),
            LoopRpcHandler(ctx),
            AuthRpcHandler(ctx),
            SessionRpcHandler(ctx),
            SessionHistoryRpcHandler(ctx),
            SchedulerRpcHandler(ctx),
            SkillsRpcHandler(ctx),
            ModelsRpcHandler(ctx),
            HooksRpcHandler(ctx),
            ExtensionsRpcHandler(ctx),
            NotificationsRpcHandler(ctx),
            PluginsRpcHandler(ctx),
            PlanRpcHandler(ctx),
            PanelRpcHandler(ctx),
            PoolGuardRpcHandler(ctx),
            WorkflowsRpcHandler(ctx),
            WorkspaceTrustRpcHandler(ctx),
        ]

    def has_live_workflows(self) -> bool:
        return self._workflows.has_live_runs()

    async def close(self) -> None:
        """Stop what this router started: its live workflow runs."""
        await self._workflows.close()

    def build_table(self) -> dict[str, RpcCall]:
        """Produce the dispatch table ``{method_wire_string: call}``.
        Validated for exhaustiveness against the :class:`RpcMethod`
        enum before returning."""
        table: dict[str, RpcCall] = {}
        for handler in self._handlers:
            for method, fn in handler.methods().items():
                if method in table:
                    raise RuntimeError(f"duplicate RPC binding for {method!r} across handlers")
                # ``RpcMethod`` is a ``StrEnum`` — the wire key is
                # the enum's ``.value``, which is also what
                # ``validate_rpc_table`` expects to see.
                table[method] = fn
        validate_rpc_table(table.keys())
        return table
