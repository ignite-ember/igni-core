"""``mint_approver_token`` and ``revoke_approver_tokens``.

Only igni's window reaches these (``backend/standing_policy.py``). The
token is the answer to the window that asked, and to no other connection.
"""

from __future__ import annotations

from igni_core.backend.pool_rpcs.base import PoolRefusal, PoolRpc
from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.schemas_pool import ApproverTokenMinted, ApproverTokensRevoked
from igni_core.backend.standing_policy import ApproverTokens
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod, RpcScope


class ApproverTokenRpc(PoolRpc):
    """Mints and revokes the tokens a program answers approval requests with.

    With no minter (a backend whose transport takes no token) there is
    nothing to revoke, and a mint says every connection may already answer.
    """

    def __init__(self, tokens: ApproverTokens | None) -> None:
        self._tokens = tokens

    @rpc(RpcMethod.MINT_APPROVER_TOKEN, args=NoArgs, scope=RpcScope.IGNI)
    def mint(self, args: NoArgs, request: msg.RPCRequest) -> ApproverTokenMinted:
        """Make a token that lets a program answer the agent's approval requests.

        Takes no arguments. Returns ``token``: a program that connects with it
        stands as an approver — it may answer approval requests and approve a
        plan, once each time, but saves no rule and changes no permission mode.
        The token lives in memory until igni exits or the tokens are revoked,
        and is sent only to the connection that asked.

        Fails when this igni's transport takes no token at all, so every
        connection may already answer approval requests.
        """
        token = self._tokens.mint_approver_token() if self._tokens is not None else None
        if token is None:
            raise PoolRefusal(
                f"{request.method}: this igni takes no token, so every "
                "connection may already answer approval requests"
            )
        return ApproverTokenMinted(token=token)

    @rpc(RpcMethod.REVOKE_APPROVER_TOKENS, args=NoArgs, scope=RpcScope.IGNI)
    async def revoke(self, args: NoArgs, request: msg.RPCRequest) -> ApproverTokensRevoked:
        """Forget every approver token, so no program answers approval requests any more.

        Takes no arguments. Returns ``revoked``: how many tokens were
        forgotten (``0`` when there were none, or when this igni takes no
        token). Connections that attached with a revoked token lose their
        approver standing.
        """
        revoked = await self._tokens.revoke_approver_tokens() if self._tokens is not None else 0
        return ApproverTokensRevoked(revoked=revoked)
