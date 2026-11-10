"""A signed pack's wire form: the compact JWS, its header and its claims."""

from __future__ import annotations

import base64
import binascii
import json
from typing import ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue, StrictInt, ValidationError

_Model = TypeVar("_Model", bound=BaseModel)


class PackRejected(Exception):
    """The pack is not one this install may apply, and why."""


class Base64Url:
    """Unpadded base64url, as JOSE writes it."""

    @staticmethod
    def decode(segment: str, *, what: str) -> bytes:
        try:
            return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        except (binascii.Error, ValueError) as exc:
            raise PackRejected(f"the pack's {what} is not base64url") from exc

    @classmethod
    def decode_uint(cls, segment: str, *, what: str) -> int:
        return int.from_bytes(cls.decode(segment, what=what), "big")


class PackHeader(BaseModel):
    """The protected header of a pack."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: The JWS ``typ`` igni server puts on a pack.
    TYPE: ClassVar[str] = "igni-group-policy+jwt"

    alg: str | None = None
    typ: str | None = None
    kid: str | None = None
    crit: JsonValue = None

    def check(self) -> None:
        """Refuse a ``crit`` header and anything that is not a group pack."""
        if self.crit:
            raise PackRejected("the pack has critical header parameters")
        if self.typ != self.TYPE:
            raise PackRejected(f"the JWS is a {self.typ!r}, not a group pack")


class PackClaims(BaseModel):
    """The payload of a pack, as igni server signs it."""

    model_config = ConfigDict(frozen=True, extra="ignore", coerce_numbers_to_str=True)

    #: The ``token_use`` claim on a pack.
    USE: ClassVar[str] = "group_policy"

    token_use: str | None = None
    iss: str | None = None
    sub: str | None = None
    org: str | None = None
    version: StrictInt | None = None
    no_pack: JsonValue = None
    #: Parsed into ``SignedPolicy`` only once everything else has checked out.
    policy: dict[str, JsonValue] | None = None

    @property
    def absent(self) -> bool:
        """igni server's signed "no pack for this person"."""
        return self.no_pack is True


class CompactPack(BaseModel):
    """``header.payload.signature``, split but not yet trusted."""

    model_config = ConfigDict(frozen=True)

    header_segment: str
    payload_segment: str
    signature_segment: str

    @classmethod
    def parse(cls, text: object) -> CompactPack:
        if not isinstance(text, str) or text.count(".") != 2:
            raise PackRejected("the pack is not a compact JWS (it is unsigned or truncated)")
        header, payload, signature = text.strip().split(".")
        return cls(header_segment=header, payload_segment=payload, signature_segment=signature)

    @classmethod
    def unverified_claims(cls, text: str) -> PackClaims | None:
        """The payload, unchecked — never a reason to trust anything in it."""
        try:
            return cls.parse(text).claims()
        except PackRejected:
            return None

    def header(self) -> PackHeader:
        return self._decode(PackHeader, self.header_segment, what="header")

    def claims(self) -> PackClaims:
        return self._decode(PackClaims, self.payload_segment, what="payload")

    def signing_input(self) -> bytes:
        try:
            return f"{self.header_segment}.{self.payload_segment}".encode("ascii")
        except UnicodeEncodeError as exc:
            raise PackRejected("the pack is not base64url") from exc

    def signature(self) -> bytes:
        return Base64Url.decode(self.signature_segment, what="signature")

    @staticmethod
    def _decode(model: type[_Model], segment: str, *, what: str) -> _Model:
        try:
            raw = json.loads(Base64Url.decode(segment, what=what))
        except ValueError as exc:
            raise PackRejected(f"the pack's {what} is not JSON") from exc
        if not isinstance(raw, dict):
            raise PackRejected(f"the pack's {what} is not an object")
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            raise PackRejected(f"the pack's {what} is malformed: {exc}") from exc
