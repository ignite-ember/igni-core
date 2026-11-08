"""Checks a freshly fetched pack before anything of it is written."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from igni_core.core.config.group_policy.answers import SignedAbsence
from igni_core.core.config.group_policy.jwks import Jwks
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.trust import PackTrust


class AcceptedPack(BaseModel):
    """A pack whose signature verified, with what it verified against."""

    model_config = ConfigDict(frozen=True)

    #: ``None`` when igni server signed "no pack for this person".
    pack: GroupPolicyPack | None
    compact: str
    jwks: Jwks
    version: int


class Verification(BaseModel):
    """The accepted pack, or why it was refused."""

    model_config = ConfigDict(frozen=True)

    accepted: AcceptedPack | None = None
    reason: str = ""


class PackVerifier:
    """Checks a fetched pack against :class:`PackTrust` and igni server's live keys.

    Used by ``GroupPolicySync.hydrate``. The keys come from
    ``jwks_fetch`` — igni server's ``/.well-known/jwks.json``, fetched now —
    narrowed to the pinned thumbprints when there are any. Without a pin the
    JWKS is trusted only over TLS (or from loopback): over plain HTTP it
    could be swapped in transit together with the pack.
    """

    def __init__(self, trust: PackTrust, jwks_fetch: Callable[[], Awaitable[Jwks | None]]) -> None:
        self._trust = trust
        self._jwks_fetch = jwks_fetch

    async def __call__(self, answer: GroupPolicyPack | SignedAbsence) -> Verification:
        compact = answer.signed_pack
        if not compact:
            return Verification(reason="igni server sent the pack unsigned")
        if not self._trust.pins and not self._trust.may_fetch_unpinned_keys():
            return Verification(
                reason=f"{self._trust.api_url!r} is not HTTPS, so igni server's keys cannot be "
                "fetched safely; pin one in group_policy.trusted_keys"
            )
        jwks = await self._jwks_fetch()
        if jwks is None:
            return Verification(reason="igni server's signing keys could not be fetched")
        verdict = self._trust.verify(compact, jwks)
        if verdict.verified is None:
            return Verification(reason=verdict.reason)
        verified = verdict.verified
        if isinstance(answer, SignedAbsence) != verified.absent:
            return Verification(reason="the signed answer does not say what the response says")
        pack = (
            None
            if verified.policy is None
            else GroupPolicyPack.from_signed(verified.policy, etag=answer.etag, signed_pack=compact)
        )
        return Verification(
            accepted=AcceptedPack(pack=pack, compact=compact, jwks=jwks, version=verified.version)
        )
