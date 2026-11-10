"""The igni server this install is configured for, as a pack's issuer."""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict


class IgniServerUrl(BaseModel):
    """``api_url``: what a pack's ``iss`` must be, and where its keys are published."""

    model_config = ConfigDict(frozen=True)

    #: Plain HTTP is trusted for keys only from a developer's own machine.
    LOOPBACK_HOSTS: ClassVar[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})

    url: str | None = None

    @property
    def normalised(self) -> str:
        """``scheme://host[:port][/path]``, lower-cased host, no trailing slash."""
        if not self.url:
            return ""
        parsed = urlparse(self.url.strip())
        if not parsed.scheme or not parsed.netloc:
            return self.url.strip().rstrip("/")
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"

    def jwks_url(self) -> str:
        """Where igni server publishes the keys it signs packs with."""
        return f"{self.normalised}/.well-known/jwks.json"

    def may_trust_unpinned_jwks(self) -> bool:
        """Whether a JWKS fetched from here may be trusted without a pin.

        Over TLS, yes. Over plain HTTP only to loopback (a developer's own
        server): anywhere else the keys could be swapped in transit along with
        the pack, so an install on a plain-HTTP network has to pin a key.
        """
        parsed = urlparse(self.url or "")
        if parsed.scheme == "https":
            return True
        return parsed.scheme == "http" and (parsed.hostname or "") in self.LOOPBACK_HOSTS
