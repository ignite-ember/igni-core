"""Auth (login + cloud-credential) RPC handlers."""

from __future__ import annotations

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_auth import (
    DiscoverServerArgs,
    ServerChoice,
    ServerFound,
    SetServerArgs,
)
from igni_core.backend.schemas_rpc import CloudPlan, LoginStarted
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class AuthRpcHandler(RpcHandler):
    """Login + credential-refresh + credential-clear + server choice."""

    @rpc(
        RpcMethod.LOGIN,
        args=NoArgs,
        scope=RpcScope.IGNI,
        streams=(
            PushChannel.LOGIN_STATUS,
            PushChannel.LOGIN_RESULT,
            PushChannel.NOTIFICATIONS_CHANGED,
        ),
    )
    async def login_start(self, args: NoArgs) -> LoginStarted:
        """Start signing in to igni server through the person's browser.

        Takes no arguments, and answers at once with ``started: true``; a
        sign-in already in progress is cancelled first. The sign-in itself
        goes on in the background: ``login_status`` pushes say what it is
        doing (the local callback server starting, the URL to open, the
        account being read), and one ``login_result`` push ends it, with
        ``success`` and, in ``result``, the signed-in email or the reason it
        failed. On success the person's notifications are read and
        ``notifications_changed`` follows.
        """
        return await self._ctx.login.start()

    @rpc(
        RpcMethod.RELOAD_CLOUD_CREDENTIALS,
        duration=RpcDuration.IO,
        args=NoArgs,
        scope=RpcScope.SESSION,
    )
    def reload_creds(self, args: NoArgs) -> msg.StatusUpdate:
        """Read the saved sign-in again, so this session acts as whoever is signed in now.

        Takes no arguments. Use it after a sign-in made elsewhere (another
        session, another window). Returns the session's status afterwards —
        model, cloud connection, account, context size, permission mode — as
        a ``status_update`` message, sent in place of an ``rpc_response``.
        """
        return self._controllers.auth.reload_cloud_credentials()

    @rpc(
        RpcMethod.CLEAR_CLOUD_CREDENTIALS,
        duration=RpcDuration.IO,
        args=NoArgs,
        scope=RpcScope.SESSION,
        streams=(PushChannel.NOTIFICATIONS_CHANGED,),
    )
    async def clear_creds(self, args: NoArgs) -> msg.StatusUpdate:
        """Sign this session out of igni server.

        Takes no arguments. The session forgets its cloud credentials, and
        the signed-in person's notifications stop arriving and leave the
        badges (``notifications_changed``). Returns the session's status
        afterwards as a ``status_update`` message, sent in place of an
        ``rpc_response``.
        """
        status = self._controllers.auth.clear_cloud_credentials()
        # Signed out: the last person's notifications stop arriving and
        # leave the badges.
        if self._ctx.notifications is not None:
            await self._ctx.notifications.signed_out()
        return status

    @rpc(
        RpcMethod.GET_CLOUD_PLAN, duration=RpcDuration.NETWORK, args=NoArgs, scope=RpcScope.SESSION
    )
    async def get_cloud_plan(self, args: NoArgs) -> CloudPlan | None:
        """Read who is signed in to igni server, and their organisation's plan.

        Takes no arguments. Asks igni server with this session's sign-in and
        returns the person's ``email`` and ``full_name``, the organisation's
        display name (``org_name``) and plan ``tier``; any of them may be
        null when igni server does not say. Returns ``null`` when nobody is
        signed in or igni server could not be asked.
        """
        return await self._controllers.auth.get_cloud_plan()

    @rpc(
        RpcMethod.DISCOVER_SERVER,
        duration=RpcDuration.NETWORK,
        args=DiscoverServerArgs,
        scope=RpcScope.IGNI,
    )
    async def discover_server(self, args: DiscoverServerArgs) -> ServerFound:
        """Find the igni server an email address's organisation runs.

        ``email`` is the person's work address. igni reads the record the
        organisation publishes on the address's domain (a well-known URL,
        then DNS) and changes nothing. Returns ``api_url`` and ``portal_url``,
        with ``source`` saying where they were found; when nothing usable
        was found both are empty and ``reason`` says why.
        """
        return await self._controllers.auth.discover_server(args.email)

    @rpc(RpcMethod.SET_SERVER, duration=RpcDuration.IO, args=SetServerArgs, scope=RpcScope.IGNI)
    def set_server(self, args: SetServerArgs) -> ServerChoice:
        """Choose the igni server to sign in to, and remember it for the next launch.

        ``api_url`` is the server's API address (``https`` only);
        ``portal_url`` its portal, empty to derive it from ``api_url``.
        Returns the server as stored, or, when the choice is refused (an
        empty or non-``https`` address), ``error`` saying why and nothing
        stored. Fails when the user configuration file cannot be read or
        written.
        """
        return self._controllers.auth.set_server(args.api_url, args.portal_url)
