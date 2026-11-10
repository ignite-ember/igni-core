"""``host.account``: who is signed in to igni.

Never the session, never a credential: the four words of the account
and nothing else.
"""

from __future__ import annotations

from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.values import Account
from igni_core.protocol.rpc import RpcMethod


class AccountMembers(Members):
    @get("account.status")
    async def status(self, c: Call) -> Account:
        return await self.host.gateway.account(c.session_id)

    @call("account.sign_in")
    async def sign_in(self, c: Call) -> None:
        # Starts the person's own sign-in. What it produces — a URL, a
        # code, a session — is theirs, so nothing of it is returned.
        await self.rpc(RpcMethod.LOGIN, c=c)

    @call("account.sign_out")
    async def sign_out(self, c: Call) -> Account:
        await self.rpc(RpcMethod.CLEAR_CLOUD_CREDENTIALS, c=c)
        return await self.host.gateway.account(c.session_id)


__all__ = ["AccountMembers"]
