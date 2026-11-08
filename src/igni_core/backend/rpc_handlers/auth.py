"""Auth (login + cloud-credential) RPC handlers."""

from __future__ import annotations

from igni_core.backend.rpc_handlers.base import RpcHandler, rpc
from igni_core.backend.schemas_auth import (
    DiscoverServerArgs,
    ServerChoice,
    ServerFound,
    SetServerArgs,
)
from igni_core.backend.schemas_rpc import LoginStarted
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class AuthRpcHandler(RpcHandler):
    """Login + credential-refresh + credential-clear + server choice."""

    @rpc(RpcMethod.LOGIN)
    async def login_start(self, args: dict) -> LoginStarted:
        return await self._ctx.login.start()

    @rpc(RpcMethod.RELOAD_CLOUD_CREDENTIALS)
    def reload_creds(self, args: dict) -> msg.StatusUpdate:
        return self._controllers.auth.reload_cloud_credentials()

    @rpc(RpcMethod.CLEAR_CLOUD_CREDENTIALS)
    async def clear_creds(self, args: dict) -> msg.StatusUpdate:
        status = self._controllers.auth.clear_cloud_credentials()
        # Signed out: the last person's notifications stop arriving and
        # leave the badges.
        if self._ctx.notifications is not None:
            await self._ctx.notifications.signed_out()
        return status

    @rpc(RpcMethod.DISCOVER_SERVER, args=DiscoverServerArgs)
    async def discover_server(self, args: DiscoverServerArgs) -> ServerFound:
        return await self._controllers.auth.discover_server(args.email)

    @rpc(RpcMethod.SET_SERVER, args=SetServerArgs)
    def set_server(self, args: SetServerArgs) -> ServerChoice:
        return self._controllers.auth.set_server(args.api_url, args.portal_url)
