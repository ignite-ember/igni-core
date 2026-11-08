"""Is this group-policy pack one igni server issued, for this person?

igni server sends each person's group pack twice in one response: as the
plain fields it always had, and as ``signed_pack`` — a compact JWS over the
same content, signed with the key igni server signs its tokens with and
publishes at ``/.well-known/jwks.json`` (igni__server
``app/services/portal/group_pack_signature.py``). igni stores that string
exactly as received (``<data_dir>/group-policy/pack.jws``) and applies a
pack only after :class:`PackSignatureVerifier` has accepted it.

## What is checked

* **The signature**, RS256 or ES256, against a trusted key (below). The
  header's ``alg`` must agree with the key it is checked against, ``typ``
  must say this is a group pack, and a ``crit`` header is refused.
* **``iss``** — the igni server this install is configured for
  (``api_url``). A pack from another server is not this one's.
* **``sub``** — the person signed in here (their session token's ``sub``).
  One person's pack is not replayable as another's.
* **``version``** — not older than the newest pack this client has already
  accepted. An older pack, even a genuine one, is a rollback.
* **``org``** — when the signed-in session names an organisation, the
  pack's must be the same one.
* **``no_pack: true``** with ``policy: null`` is igni server's signed "this
  person has no pack"; it verifies like a pack and means "clear the cache".
* **``token_use``** is ``group_policy`` and the payload carries a
  ``policy`` object. There is no ``exp``: a pack keeps working offline.

## Which keys are trusted

1. **Pinned by an administrator** — ``group_policy.trusted_keys``: RFC 7638
   thumbprints of igni server's signing keys (the ``kid`` igni server gives
   a key it generated *is* its thumbprint, but the thumbprint is computed
   here, never read from the ``kid``). Honoured only from the
   managed-settings file or ``IGNI_GROUP_POLICY_TRUSTED_KEYS``
   (:class:`~igni_core.core.config.merge_plan.ManagedOnlyTier`). The key
   material still comes from a JWKS — live, or the copy cached beside the
   pack — but only a key whose thumbprint is pinned is used, so a cached
   JWKS anybody could have rewritten cannot introduce a key.
2. **Otherwise, the JWKS igni server serves**, fetched over TLS when the
   pack is fetched, and cached beside it for offline starts. Without a pin
   that cached copy is only as trustworthy as the directory it is in: it
   stops a repository or a stray file from planting a policy, and it does
   **not** stop the person who owns the machine (or malware running as
   them) from replacing both the pack and the keys. Pinning is what closes
   that.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError

from igni_core.core.config.group_policy.issuer import IgniServerUrl
from igni_core.core.config.group_policy.jwks import Jwks
from igni_core.core.config.group_policy.jws import (
    CompactPack,
    PackClaims,
    PackHeader,
    PackRejected,
)
from igni_core.core.config.group_policy.pack import SignedPolicy
from igni_core.core.config.group_policy.signing_key import SigningKey


class VerifiedPack(BaseModel):
    """A pack :class:`PackSignatureVerifier` accepted."""

    model_config = ConfigDict(frozen=True)

    #: The pack's content, or ``None`` when igni server signed "this person
    #: has no pack" (:attr:`absent`).
    policy: SignedPolicy | None
    issuer: str
    subject: str
    org: str | None
    version: int
    kid: str
    pinned: bool
    #: igni server's signed "no pack for this person": clear what is cached.
    absent: bool = False


class PackSignatureVerifier(BaseModel):
    """Whether a pack is genuine and meant for this install.

    Holds what a pack is checked against: the issuer, the person signed in
    (and their organisation, when the session names one) and the pinned key
    thumbprints. Every check raises :class:`PackRejected` naming what failed.
    """

    model_config = ConfigDict(frozen=True)

    issuer: IgniServerUrl
    subject: str | None
    #: When the signed-in session names one, the pack's ``org`` must match.
    org: str | None = None
    #: When any are given, only keys with these thumbprints are used.
    pins: tuple[str, ...] = ()

    def verify(
        self, text: str, jwks: Jwks | None, *, min_version: int | None = None
    ) -> VerifiedPack:
        """The pack's content, if it is genuine and meant for this install.

        ``jwks`` supplies key material. ``min_version`` is the newest version
        already accepted: an older pack is a rollback.
        """
        compact = CompactPack.parse(text)
        header = compact.header()
        header.check()
        self._check_signature(compact, header, jwks)
        claims = compact.claims()
        issuer, subject = self._check_audience(claims)
        version = self._check_version(claims, min_version)
        org = self._check_org(claims)
        return VerifiedPack(
            policy=self._policy(claims),
            issuer=issuer,
            subject=subject,
            org=org,
            version=version,
            kid=header.kid or "",
            pinned=bool(self.pins),
            absent=claims.absent,
        )

    def _trusted_keys(self, jwks: Jwks | None) -> list[SigningKey]:
        keys = SigningKey.all_in(jwks)
        if self.pins:
            keys = [key for key in keys if key.thumbprint in self.pins]
            if not keys:
                raise PackRejected(
                    "none of the keys pinned in group_policy.trusted_keys is available to check "
                    "the pack against"
                )
        if not keys:
            raise PackRejected("there is no igni server key to check the pack against")
        return keys

    def _check_signature(self, compact: CompactPack, header: PackHeader, jwks: Jwks | None) -> None:
        if not SigningKey.accepts(header.alg):
            raise PackRejected(
                f"the pack is signed with {header.alg!r}; only RS256 and ES256 are accepted"
            )
        keys = self._trusted_keys(jwks)
        kid = header.kid or ""
        candidates = [k for k in keys if k.algorithm == header.alg and (not kid or k.kid == kid)]
        signing_input = compact.signing_input()
        signature = compact.signature()
        if not any(key.verifies(signing_input, signature) for key in candidates):
            raise PackRejected(
                "the pack's signature does not verify against "
                + ("a pinned key" if self.pins else "igni server's keys")
            )

    def _check_audience(self, claims: PackClaims) -> tuple[str, str]:
        """The pack is a group pack, from this igni server, for this person."""
        if claims.token_use != PackClaims.USE:
            raise PackRejected("the signed object is not a group pack")
        expected_issuer = self.issuer.normalised
        if not expected_issuer:
            raise PackRejected("api_url is not set, so the pack's issuer cannot be checked")
        if IgniServerUrl(url=claims.iss).normalised != expected_issuer:
            raise PackRejected(
                f"the pack was issued by {claims.iss!r}, not by this install's igni server "
                f"({expected_issuer})"
            )
        if not self.subject:
            raise PackRejected("nobody is signed in, so the pack cannot be matched to a person")
        if (claims.sub or "") != self.subject:
            raise PackRejected("the pack was issued for somebody other than the person signed in")
        return expected_issuer, self.subject

    @staticmethod
    def _check_version(claims: PackClaims, min_version: int | None) -> int:
        if claims.version is None:
            raise PackRejected("the pack carries no version")
        if min_version is not None and claims.version < min_version:
            raise PackRejected(
                f"the pack is older (version {claims.version}) than one this install has already "
                f"applied (version {min_version}); a rollback is refused"
            )
        return claims.version

    def _check_org(self, claims: PackClaims) -> str | None:
        pack_org = claims.org or None
        if self.org and pack_org and pack_org != self.org:
            raise PackRejected(
                f"the pack is for organisation {pack_org!r}, not the one signed in here "
                f"({self.org!r})"
            )
        return pack_org

    @staticmethod
    def _policy(claims: PackClaims) -> SignedPolicy | None:
        if claims.absent:
            return None
        if claims.policy is None:
            raise PackRejected("the pack carries no policy")
        try:
            return SignedPolicy.model_validate(claims.policy)
        except ValidationError as exc:
            raise PackRejected(f"the signed pack does not parse: {exc}") from exc
