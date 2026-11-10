"""The wire models of the distribution endpoints.

What igni server sends, and what one sync did — as models, so a
malformed listing is a validation error rather than a ``KeyError``
three calls later, and so the publisher's keys arrive typed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PublisherJwk(BaseModel):
    """One public key from a publisher's JWK set.

    The fields igni reads are typed; anything else the publisher
    sends is kept, so a key that carries more than igni knows about
    still arrives intact. Every field is optional because a JWK is
    only usable when it names the algorithm it signs with — a key
    igni cannot use is refused when the signature is checked, not
    when the listing is read.
    """

    model_config = ConfigDict(extra="allow")

    #: Which key signed, when the publisher has more than one.
    kid: str = ""
    #: The key type: ``RSA`` or ``EC``.
    kty: str = ""
    #: The algorithm, when the key states one.
    alg: str | None = None
    #: RSA: the modulus.
    n: str | None = None
    #: RSA: the exponent.
    e: str | None = None
    #: EC: the curve — igni accepts P-256 only.
    crv: str | None = None
    #: EC: the x coordinate.
    x: str | None = None
    #: EC: the y coordinate.
    y: str | None = None


class PublisherJwkSet(BaseModel):
    """The publisher's public keys, as the listing carries them."""

    model_config = ConfigDict(extra="allow")

    keys: list[PublisherJwk] = Field(default_factory=list)

    def as_jwks(self) -> dict[str, Any]:
        """The set as plain JSON, for the signature verifier.

        The lowering happens here, in one place: the verifier works
        on the JSON shape, because that is what a JWS is checked
        against.
        """
        return self.model_dump(exclude_none=True)


class DistributedExtension(BaseModel):
    """One row of ``GET /v1/extensions``."""

    model_config = ConfigDict(extra="ignore")

    extension_id: str
    version: str
    name: str = ""
    description: str = ""
    #: The service it belongs to — and the audience of its tokens.
    service_url: str
    sha256: str
    signature: str = ""
    #: The keys the bundle's signature is checked against.
    publisher_jwks: PublisherJwkSet
    published_at: datetime | None = None
    #: Every group in the person's organisation it is switched on for
    #: (igni server's §8.3).
    groups: list[str] = Field(default_factory=list)


class DistributionListing(BaseModel):
    """What igni server lists for the signed-in person."""

    model_config = ConfigDict(extra="ignore")

    #: The one group the signed-in person belongs to, read by igni
    #: server as it listed; ``None`` when they are in none.
    group: str | None = None
    #: That group's name, to show a person; absent from an older igni server.
    group_name: str | None = None
    extensions: list[DistributedExtension] = Field(default_factory=list)
    #: The super admin's priority order of extensions, highest first:
    #: it settles every collision (:mod:`igni_core.extensions.impact`)
    #: and is the order edits apply and extensions' hooks run in.
    priority: list[str] = Field(default_factory=list)

    def entitled(self) -> list[DistributedExtension]:
        """What the person gets: each extension switched on for their group.

        igni server lists nothing else (its §8.3); this is the machine's
        own check of the same rule, so an extension reaches only the
        people whose group an org admin switched it on for, whatever a
        listing says.
        """
        if self.group is None:
            return []
        return [entry for entry in self.extensions if self.group in entry.groups]


class DownloadedBundle(BaseModel):
    """One bundle, as it came off the wire."""

    archive: bytes
    signature: str
    sha256: str = ""


class SyncReport(BaseModel):
    """What one sync did, for the log and for tests."""

    #: Why nothing was attempted, when nothing was.
    skipped: str | None = None
    installed: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    #: Development installs a listed extension did not replace.
    development: list[str] = Field(default_factory=list)
    #: Whether the priority order changed.
    priority_changed: bool = False
    failed: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "DistributedExtension",
    "DistributionListing",
    "DownloadedBundle",
    "PublisherJwk",
    "PublisherJwkSet",
    "SyncReport",
]
