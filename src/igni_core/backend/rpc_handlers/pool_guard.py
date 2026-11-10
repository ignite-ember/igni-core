"""Pool-level RPC guard handler.

The pool-level RPCs (:data:`POOL_LEVEL_RPCS`) are answered by
:class:`~igni_core.backend.pool_rpcs.PoolRpcRouter` BEFORE per-runtime
dispatch. If any code path skips that and dispatches one of these
methods through a runtime's RPC table, the guard raises a loud
:class:`RuntimeError` rather than silently succeeding.

The two subsets beside it say who may reach a pool-level call:
:data:`PAGE_RPCS` an extension's page, :data:`PLATFORM_RPCS` only the
platform (``backend/message_admission.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.protocol.rpc import RpcMethod

# Frozen so callers (the pool's admission and RPC router, rpc_router) can
# `in`-check against it without worrying about accidental mutation.
POOL_LEVEL_RPCS: frozenset[RpcMethod] = frozenset(
    {
        RpcMethod.ATTACH_SESSION,
        RpcMethod.HOST_MODEL,
        RpcMethod.WINDOW_MODEL,
        RpcMethod.EXTENSION_SERVICE_TOKEN,
        RpcMethod.DELIVER_SERVICE_EVENTS,
        RpcMethod.VM_SIGN_IN,
        RpcMethod.MINT_APPROVER_TOKEN,
        RpcMethod.REVOKE_APPROVER_TOKENS,
    }
)

#: What a message speaking for an extension may reach besides its own
#: ``ext.<id>.*`` and the base methods its manifest grants.
PAGE_RPCS: frozenset[RpcMethod] = frozenset(
    {RpcMethod.HOST_MODEL, RpcMethod.EXTENSION_SERVICE_TOKEN}
)

#: Calls only the platform makes: a layer may take them from its control
#: connection alone (``Layer.platform_calls_need_control``).
PLATFORM_RPCS: frozenset[RpcMethod] = frozenset(
    {RpcMethod.DELIVER_SERVICE_EVENTS, RpcMethod.VM_SIGN_IN}
)


class PoolGuardRpcHandler(RpcHandler):
    """Single ``_pool_level_guard`` method registered under every
    pool-level RPC key: these must never reach a runtime's table."""

    def methods(self) -> dict[RpcMethod, Callable[[dict], Any]]:
        return {method: self._pool_level_guard for method in POOL_LEVEL_RPCS}

    def _pool_level_guard(self, args: dict) -> None:
        raise RuntimeError(
            "pool-level RPC dispatched through per-runtime table — "
            "PoolRpcRouter must answer every method in POOL_LEVEL_RPCS "
            "before this point"
        )
