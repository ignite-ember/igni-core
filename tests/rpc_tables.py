"""The RPC dispatch table for one backend, built without booting a :class:`BackendApp`.

Tests that call individual RPC handlers build the table the way the
app does — a :class:`PushNotificationBridge`, a :class:`LoginCoordinator`
over it, and an :class:`RpcRouter` over both — and index it by
:class:`RpcMethod`.
"""

from __future__ import annotations

from typing import Any

from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.rpc_binding import RpcCall
from igni_core.backend.rpc_router import RpcRouter


def rpc_table_for(backend: Any, transport: Any) -> dict[str, RpcCall]:
    """The dispatch table a router over ``backend`` and ``transport`` builds.

    The bridge has no loop: the handlers these tests reach do not push.
    """
    push = PushNotificationBridge(transport=transport, loop=None, queue=[])
    login = LoginCoordinator(backend=backend, push_bridge=push)
    return RpcRouter(backend=backend, login=login, push=push).build_table()
