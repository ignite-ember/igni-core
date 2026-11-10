"""Hooks (session-lifecycle + tool-gate) RPC handlers."""

from __future__ import annotations

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_panels import HookEntryView
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope


class HooksRpcHandler(RpcHandler):
    """Details view + reload for the currently-registered hooks."""

    @rpc(RpcMethod.GET_HOOKS_DETAILS, args=NoArgs, scope=RpcScope.SESSION)
    def get_hooks_details(self, args: NoArgs) -> list[HookEntryView]:
        """List the hooks this session runs.

        Takes no arguments. Returns one row per hook: the ``event`` it runs
        on, its ``type`` (a shell ``command`` or an HTTP ``url``), the
        ``matcher`` that narrows it, its timeout, whether it runs in the
        background, and the HTTP headers it sends.
        """
        return self._ctx.backend.get_hooks_details()

    @rpc(RpcMethod.RELOAD_HOOKS, duration=RpcDuration.IO, args=NoArgs, scope=RpcScope.SESSION)
    def reload_hooks(self, args: NoArgs) -> msg.Info:
        """Read this session's hook configuration again.

        Takes no arguments. Returns an ``info`` message, sent in place of an
        ``rpc_response``, saying how many hooks are now active.
        """
        return self._controllers.panels.reload_hooks()
