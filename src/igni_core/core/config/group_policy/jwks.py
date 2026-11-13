"""igni server's published signing keys, as a group pack is checked against them."""

from __future__ import annotations

import base64
import hashlib
import json

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError, field_validator

from igni_core.core.config.group_policy.jws import PackRejected


class Jwk(BaseModel):
    """One public key. Unknown members are kept, so the JWKS is cached as sent."""

    model_config = ConfigDict(frozen=True, extra="allow")

    kty: str | None = None
    kid: str | None = None
    alg: str | None = None
    use: str | None = None
    crv: str | None = None
    x: str | None = None
    y: str | None = None
    n: str | None = None
    e: str | None = None

    def thumbprint(self) -> str:
        """RFC 7638: SHA-256 over the key's required members, base64url.

        The same computation igni server's ``PublicJwk.thumbprint`` makes, so
        an administrator can pin the value the portal shows (and the ``kid``
        of a generated key).
        """
        if self.kty == "RSA":
            members = {"e": self.e, "kty": "RSA", "n": self.n}
        elif self.kty == "EC":
            members = {"crv": self.crv, "kty": "EC", "x": self.x, "y": self.y}
        else:
            raise PackRejected(f"a key of type {self.kty!r} is not RSA or EC")
        canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode()


class Jwks(BaseModel):
    """A JWKS — ``/.well-known/jwks.json``, live or the copy cached beside the pack."""

    model_config = ConfigDict(frozen=True)

    keys: list[Jwk] = []

    @field_validator("keys", mode="before")
    @classmethod
    def _skip_entries_that_are_not_keys(cls, value: object) -> object:
        # A malformed entry costs that key, not the whole set — the same
        # leniency ``SigningKey.all_in`` applies to each key it reads.
        if not isinstance(value, list):
            return []
        return [entry for entry in value if cls._is_key(entry)]

    @staticmethod
    def _is_key(entry: object) -> bool:
        try:
            Jwk.model_validate(entry)
        except ValidationError:
            return False
        return True

    @classmethod
    def parse(cls, raw: object) -> Jwks | None:
        """A JWKS from decoded JSON, or ``None`` when it is not one."""
        if not isinstance(raw, dict):
            return None
        try:
            return cls.model_validate(raw)
        except ValidationError:
            return None

    def as_mapping(self) -> dict[str, JsonValue]:
        """The JWKS as JSON — what ``jwks.json`` holds."""
        return self.model_dump(exclude_none=True)
