"""What a pack is checked against on this install."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.auth.credentials import CredentialsStore
from igni_core.core.auth.schemas import SignedInSession
from igni_core.core.config.group_policy.issuer import IgniServerUrl
from igni_core.core.config.group_policy.jwks import Jwks
from igni_core.core.config.group_policy.jws import PackRejected
from igni_core.core.config.group_policy.signature import PackSignatureVerifier, VerifiedPack
from igni_core.core.config.settings import Settings


class PackVerdict(BaseModel):
    """Whether a compact JWS is a pack this install may apply, and why not."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    verified: VerifiedPack | None = None
    reason: str = ""


class PackTrust(BaseModel):
    """The issuer, the person, the pinned keys — what a pack is checked against."""

    model_config = ConfigDict(frozen=True)

    #: The igni server this install is configured for — the pack's ``iss``.
    api_url: str | None = None
    #: The person signed in — the pack's ``sub`` and ``org``.
    session: SignedInSession = Field(default_factory=SignedInSession)
    #: Thumbprints from ``group_policy.trusted_keys`` (managed or env only).
    pins: tuple[str, ...] = ()
    #: ``group_policy.required``.
    required: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> PackTrust:
        """The trust for ``settings``.

        A field that is not of its declared type — a caller holding a stub
        settings object — is *ignored* rather than coerced, the same way
        :meth:`GroupPolicyLayout.for_data_dir` treats ``data_dir``.
        """
        credentials_file = settings.auth.credentials_file
        store = CredentialsStore(credentials_file if isinstance(credentials_file, str) else None)
        api_url = settings.api_url
        trusted_keys = settings.group_policy.trusted_keys
        required = settings.group_policy.required
        return cls(
            api_url=api_url if isinstance(api_url, str) and api_url else None,
            session=store.signed_in_session(),
            pins=tuple(k for k in trusted_keys if isinstance(k, str))
            if isinstance(trusted_keys, (list, tuple))
            else (),
            required=required if isinstance(required, bool) else False,
        )

    def may_fetch_unpinned_keys(self) -> bool:
        """Whether a JWKS fetched from ``api_url`` may be trusted without a pin.

        Over TLS, or from loopback. Anywhere else over plain HTTP the keys
        could be swapped in transit together with the pack.
        """
        return IgniServerUrl(url=self.api_url).may_trust_unpinned_jwks()

    def signature_verifier(self) -> PackSignatureVerifier:
        """The verifier holding this install's issuer, person and pins."""
        return PackSignatureVerifier(
            issuer=IgniServerUrl(url=self.api_url),
            subject=self.session.subject,
            org=self.session.org,
            pins=self.pins,
        )

    def verify(
        self, compact: str, jwks: Jwks | None, *, min_version: int | None = None
    ) -> PackVerdict:
        """Check ``compact`` against ``jwks`` (narrowed to the pins) for this install.

        The one place a :class:`PackRejected` is turned into a verdict.
        """
        try:
            verified = self.signature_verifier().verify(compact, jwks, min_version=min_version)
        except PackRejected as exc:
            return PackVerdict(ok=False, reason=str(exc))
        return PackVerdict(ok=True, verified=verified)
