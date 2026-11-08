"""``extension_service_token``: an extension page's token for its own service."""

from __future__ import annotations

from igni_core.backend.pool_rpcs.base import PoolRpc, UnavailableRpc
from igni_core.backend.schemas_pool import PoolAnswer
from igni_core.extensions.service_token import PageServiceTokens
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class PageTokenRpc(PoolRpc):
    """``bridge.service`` asks; igni's window forwards it with the frame's extension
    as ``caller``. The audience is that extension's own service and nothing it
    asked for; "nobody is signed in" is a state the page shows, so it is a result
    (``ok: false``), not an error."""

    methods = frozenset({RpcMethod.EXTENSION_SERVICE_TOKEN})

    PAGE_ONLY = "extension_service_token: only an extension's page asks for one"

    def __init__(self, tokens: PageServiceTokens) -> None:
        self._tokens = tokens

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [UnavailableRpc(RpcMethod.EXTENSION_SERVICE_TOKEN, cls.PAGE_ONLY)]

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        if not request.caller:
            return PoolAnswer.failed(self.PAGE_ONLY)
        return PoolAnswer.ok(await self._tokens.reply_for(request.caller))
