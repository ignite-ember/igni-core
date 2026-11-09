"""Workspace-trust RPC handlers: the window's "Trust this folder?" dialog.

``get_workspace_trust`` → :class:`WorkspaceTrustStatus` for this runtime's
folder. ``set_workspace_trust`` with ``{"decision": "trust" | "distrust" |
"once"}`` records the answer ("once" lasts until the backend exits), pushes
the new status on ``workspace_trust_required`` and, when trust changed,
rebuilds the running session so the folder's tools, hooks, plugins, language
servers, monitors and config come in (or go). A refusal by the organisation's
policy fails the RPC with the policy's reason.

Not part of the extension host model: trusting a folder is the person's
decision (``coverage.RPC_UNREACHABLE``).
"""

from __future__ import annotations

from typing import Any, Literal

from igni_core.backend.rpc_binding import NoArgs, RpcArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.workspace_trust_controller import WorkspaceTrustStatus
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class TrustDecisionArgs(RpcArgs):
    """``set_workspace_trust``: the person's answer to "Trust this folder?"."""

    #: ``trust``: trust the folder and remember it. ``once``: trust it until
    #: igni exits. ``distrust``: do not trust it, and remember that.
    decision: Literal["trust", "distrust", "once"]


class WorkspaceTrustRpcHandler(RpcHandler):
    """Read and answer the folder's workspace-trust decision."""

    @rpc(
        RpcMethod.GET_WORKSPACE_TRUST, duration=RpcDuration.IO, args=NoArgs, scope=RpcScope.SESSION
    )
    def get_workspace_trust(self, args: NoArgs) -> WorkspaceTrustStatus:
        """Read whether this session's project folder is trusted.

        Takes no arguments. Returns the folder's trust ``state``
        (``trusted``, ``untrusted``, or ``ask`` when the person has not
        decided), whether it ``can_trust`` it under the organisation's
        policy and, if not, the ``reason``, plus where the decision came
        from. Until a folder is trusted its own tools, hooks, plugins and
        configuration are not loaded.
        """
        return self._ctx.backend.workspace_trust.status()

    @rpc(
        RpcMethod.SET_WORKSPACE_TRUST,
        duration=RpcDuration.IO,
        args=TrustDecisionArgs,
        scope=RpcScope.SESSION,
        streams=(PushChannel.WORKSPACE_TRUST_REQUIRED,),
    )
    async def set_workspace_trust(self, args: TrustDecisionArgs) -> WorkspaceTrustStatus:
        """Answer "Trust this folder?" for this session's project folder.

        ``decision`` is ``trust`` (remembered), ``once`` (until igni exits)
        or ``distrust``. When trust changed, the session is rebuilt so the
        folder's tools, hooks, plugins, language servers and configuration
        come in or go. Every window showing the dialog for that folder is
        told the new status (``workspace_trust_required``). Returns the new
        status. Fails when the organisation's policy does not let the folder
        be trusted.
        """
        backend: Any = self._ctx.backend
        status, changed = backend.workspace_trust.answer(args.decision)
        if changed:
            await backend.apply_workspace_trust()
        push = self._ctx.push
        if push is not None:
            push.workspace_trust(status)
        return status
