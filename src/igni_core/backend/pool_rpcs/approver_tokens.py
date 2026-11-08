"""``mint_approver_token`` and ``revoke_approver_tokens``.

Only igni's window reaches these (``backend/standing_policy.py``). The
token is the answer to the window that asked, and to no other connection.
"""

from __future__ import annotations

from igni_core.backend.pool_rpcs.base import PoolRpc
from igni_core.backend.schemas_pool import ApproverTokenMinted, ApproverTokensRevoked, PoolAnswer
from igni_core.backend.standing_policy import ApproverTokens
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class ApproverTokenRpc(PoolRpc):
    """Mints and revokes the tokens a program answers approval requests with.

    With no minter (a backend whose transport takes no token) there is
    nothing to revoke, and a mint says every connection may already answer.
    """

    methods = frozenset({RpcMethod.MINT_APPROVER_TOKEN, RpcMethod.REVOKE_APPROVER_TOKENS})

    def __init__(self, tokens: ApproverTokens | None) -> None:
        self._tokens = tokens

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        if request.method == RpcMethod.REVOKE_APPROVER_TOKENS:
            revoked = await self._tokens.revoke_approver_tokens() if self._tokens is not None else 0
            return PoolAnswer.ok(ApproverTokensRevoked(revoked=revoked))
        token = self._tokens.mint_approver_token() if self._tokens is not None else None
        if token is None:
            return PoolAnswer.failed(
                f"{request.method}: this igni takes no token, so every "
                "connection may already answer approval requests"
            )
        return PoolAnswer.ok(ApproverTokenMinted(token=token))
