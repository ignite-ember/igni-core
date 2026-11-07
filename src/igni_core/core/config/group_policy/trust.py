"""What a pack is checked against on this install."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, InstanceOf

from igni_core.core.auth.credentials import CredentialsStore
from igni_core.core.auth.schemas import SignedInSession
from igni_core.core.config.group_policy.jwks import Jwks
from igni_core.core.config.group_policy_signature import (
    PackRejected,
    VerifiedPack,
    may_trust_unpinned_jwks_from,
    verify_pack,
)
from igni_core.core.config.settings import Settings


class PackVerdict(BaseModel):
    """Whether a compact JWS is a pack this install may apply, and why not."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    verified: InstanceOf[VerifiedPack] | None = None
    reason: str = ""


class PackTrust(BaseModel):
    """The issuer, the person, the pinned keys — what ``verify_pack`` checks a pack against."""

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
        return may_trust_unpinned_jwks_from(self.api_url or "")

    def verify(
        self, compact: str, jwks: Jwks | None, *, min_version: int | None = None
    ) -> PackVerdict:
        """Check ``compact`` against ``jwks`` (narrowed to the pins) for this install.

        The one place a :class:`PackRejected` from ``verify_pack`` is turned
        into a verdict.
        """
        try:
            verified = verify_pack(
                compact,
                jwks=jwks.as_mapping() if jwks is not None else None,
                pins=self.pins,
                api_url=self.api_url,
                subject=self.session.subject,
                min_version=min_version,
                org=self.session.org,
            )
        except PackRejected as exc:
            return PackVerdict(ok=False, reason=str(exc))
        return PackVerdict(ok=True, verified=verified)
