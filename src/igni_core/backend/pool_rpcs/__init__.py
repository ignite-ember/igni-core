"""The session pool's own calls: one handler per concern, behind :class:`PoolRpcRouter`."""

from __future__ import annotations

from igni_core.backend.pool_rpcs.approver_tokens import ApproverTokenRpc
from igni_core.backend.pool_rpcs.attach_session import AttachSessionRpc
from igni_core.backend.pool_rpcs.base import PoolRpc, UnavailableRpc
from igni_core.backend.pool_rpcs.host_model import HostModelRpcs, PageHostModel
from igni_core.backend.pool_rpcs.page_token import PageTokenRpc
from igni_core.backend.pool_rpcs.platform_calls import ServiceEventRpc, VmSignIn, VmSignInRpc
from igni_core.backend.pool_rpcs.router import PoolRpcRouter

__all__ = [
    "ApproverTokenRpc",
    "AttachSessionRpc",
    "HostModelRpcs",
    "PageHostModel",
    "PageTokenRpc",
    "PoolRpc",
    "PoolRpcRouter",
    "ServiceEventRpc",
    "UnavailableRpc",
    "VmSignIn",
    "VmSignInRpc",
]
