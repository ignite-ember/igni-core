"""Per-domain RPC handler classes.

Every RPC in :class:`RpcMethod` is implemented by a method on one of
these :class:`RpcHandler` subclasses. :class:`RpcRouter` composes the
subclasses at construction and merges their :meth:`RpcHandler.methods`
submaps into the flat ``dict[str, Callable[[dict], Any]]`` dispatch
table the receive loop calls through.
"""

from __future__ import annotations

from igni_core.backend.rpc_handlers.auth import AuthRpcHandler
from igni_core.backend.rpc_handlers.base import RpcHandler, RpcHandlerContext, rpc
from igni_core.backend.rpc_handlers.extensions import ExtensionsRpcHandler
from igni_core.backend.rpc_handlers.hooks import HooksRpcHandler
from igni_core.backend.rpc_handlers.loop import LoopRpcHandler
from igni_core.backend.rpc_handlers.mcp import McpRpcHandler
from igni_core.backend.rpc_handlers.models import ModelsRpcHandler
from igni_core.backend.rpc_handlers.notifications import NotificationsRpcHandler
from igni_core.backend.rpc_handlers.panels import PanelRpcHandler
from igni_core.backend.rpc_handlers.plan import PlanRpcHandler
from igni_core.backend.rpc_handlers.plugins import PluginsRpcHandler
from igni_core.backend.rpc_handlers.pool_guard import (
    PAGE_RPCS,
    PLATFORM_RPCS,
    POOL_LEVEL_RPCS,
    PoolGuardRpcHandler,
)
from igni_core.backend.rpc_handlers.scheduler import SchedulerRpcHandler
from igni_core.backend.rpc_handlers.session import SessionRpcHandler
from igni_core.backend.rpc_handlers.session_history import SessionHistoryRpcHandler
from igni_core.backend.rpc_handlers.skills import SkillsRpcHandler
from igni_core.backend.rpc_handlers.workflows import WorkflowsRpcHandler
from igni_core.backend.rpc_handlers.workspace_trust import WorkspaceTrustRpcHandler

__all__ = [
    "PAGE_RPCS",
    "PLATFORM_RPCS",
    "POOL_LEVEL_RPCS",
    "AuthRpcHandler",
    "ExtensionsRpcHandler",
    "HooksRpcHandler",
    "LoopRpcHandler",
    "McpRpcHandler",
    "ModelsRpcHandler",
    "NotificationsRpcHandler",
    "PanelRpcHandler",
    "PlanRpcHandler",
    "PluginsRpcHandler",
    "PoolGuardRpcHandler",
    "RpcHandler",
    "RpcHandlerContext",
    "SchedulerRpcHandler",
    "SessionHistoryRpcHandler",
    "SessionRpcHandler",
    "SkillsRpcHandler",
    "WorkflowsRpcHandler",
    "WorkspaceTrustRpcHandler",
    "rpc",
]
