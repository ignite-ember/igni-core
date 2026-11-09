"""The session pool's own calls: one handler per concern, behind :class:`PoolRpcRouter`."""

from __future__ import annotations

from igni_core.backend.pool_rpcs.approver_tokens import ApproverTokenRpc
from igni_core.backend.pool_rpcs.attach_session import AttachSessionRpc
from igni_core.backend.pool_rpcs.base import PoolRefusal, PoolRpc, UnavailableRpc
from igni_core.backend.pool_rpcs.host_model import HostModelRpcs, PageHostModel
from igni_core.backend.pool_rpcs.page_token import PageTokenRpc
from igni_core.backend.pool_rpcs.platform_calls import ServiceEventRpc, VmSignIn, VmSignInRpc
from igni_core.backend.pool_rpcs.router import PoolRpcRouter

#: Every class that answers a pool-level call, whether or not this backend
#: mounts it: the methods their tagged methods implement are the pool's.
POOL_RPCS: tuple[type[PoolRpc], ...] = (
    AttachSessionRpc,
    HostModelRpcs,
    PageTokenRpc,
    VmSignInRpc,
    ServiceEventRpc,
    ApproverTokenRpc,
)

__all__ = [
    "POOL_RPCS",
    "ApproverTokenRpc",
    "AttachSessionRpc",
    "HostModelRpcs",
    "PageHostModel",
    "PageTokenRpc",
    "PoolRefusal",
    "PoolRpc",
    "PoolRpcRouter",
    "ServiceEventRpc",
    "UnavailableRpc",
    "VmSignIn",
    "VmSignInRpc",
]
