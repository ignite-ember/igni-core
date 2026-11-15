"""``extension_service_token``: an extension page's token for its own service."""

from __future__ import annotations

from pydantic import Field

from igni_core.backend.pool_rpcs.base import PoolRefusal, PoolRpc, UnavailableRpc
from igni_core.backend.rpc_binding import RpcArgs, rpc
from igni_core.extensions.service_token import PageServiceTokens, PageTokenReply
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.rpc import RpcErrorCode


class PageTokenArgs(RpcArgs):
    """The arguments of ``extension_service_token``."""

    refresh: bool = Field(
        default=False,
        description=(
            "Exchange a new token rather than answer the one igni holds: the "
            "service refused that one (a `401`), and it would refuse it again."
        ),
    )


class PageTokenRpc(PoolRpc):
    """``bridge.service`` asks; igni's window forwards it with the frame's extension
    as ``caller``. The audience is that extension's own service and nothing it
    asked for; "nobody is signed in" is a state the page shows, so it is a result
    (``ok: false``), not an error."""

    PAGE_ONLY = "extension_service_token: only an extension's page asks for one"

    def __init__(self, tokens: PageServiceTokens) -> None:
        self._tokens = tokens

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [UnavailableRpc(RpcMethod.EXTENSION_SERVICE_TOKEN, cls.PAGE_ONLY)]

    @rpc(
        RpcMethod.EXTENSION_SERVICE_TOKEN,
        duration=RpcDuration.NETWORK,
        args=PageTokenArgs,
        scope=RpcScope.IGNI,
    )
    async def token(self, args: PageTokenArgs, request: msg.RPCRequest) -> PageTokenReply:
        """Get an access token for the calling extension's own service.

        Only an extension's page asks, through igni's window, which sets the
        request's ``caller`` to the page's extension; the token's audience is
        the service recorded for that extension, never one the page names.
        ``refresh: true`` exchanges a new token instead of answering the held
        one, for a page whose service refused it.

        Returns ``ok: true`` with ``access_token``, ``expires_in`` (seconds)
        and ``service_url``; or ``ok: false`` with ``error`` when nobody is
        signed in, the extension has no service, or igni server refused the
        exchange — states a page shows rather than failures. The answer goes
        only to the connection that asked. Fails when the request has no
        ``caller`` or this igni mounts no token exchange.
        """
        if not request.caller:
            raise PoolRefusal(self.PAGE_ONLY, RpcErrorCode.FORBIDDEN)
        return await self._tokens.reply_for(request.caller, refresh=args.refresh)
