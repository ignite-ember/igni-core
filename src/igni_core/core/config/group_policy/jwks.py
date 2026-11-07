"""igni server's published signing keys, as a group pack is checked against them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError, field_validator


class Jwk(BaseModel):
    """One public key. Unknown members are kept: ``verify_pack`` reads the JWK as sent."""

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


class Jwks(BaseModel):
    """A JWKS — ``/.well-known/jwks.json``, live or the copy cached beside the pack."""

    model_config = ConfigDict(frozen=True)

    keys: list[Jwk] = []

    @field_validator("keys", mode="before")
    @classmethod
    def _skip_entries_that_are_not_keys(cls, value: object) -> object:
        # A malformed entry costs that key, not the whole set — the same
        # leniency ``keys_from_jwks`` applies to each key it reads.
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, dict)]

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
        """The JWKS as JSON — what ``verify_pack`` reads and ``jwks.json`` holds."""
        return self.model_dump(exclude_none=True)
