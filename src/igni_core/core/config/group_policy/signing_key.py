"""One of igni server's public keys, ready to check a pack's signature."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from igni_core.core.config.group_policy.jwks import Jwk, Jwks
from igni_core.core.config.group_policy.jws import Base64Url, PackRejected


class SigningKey:
    """A JWK turned into a usable RSA or P-256 public key."""

    #: The algorithm each key type may be used with — checked against the key,
    #: never chosen by the header.
    ALGORITHM_FOR_KEY_TYPE: ClassVar[Mapping[str, str]] = MappingProxyType(
        {"RSA": "RS256", "EC": "ES256"}
    )

    def __init__(self, jwk: Jwk) -> None:
        self.kid = jwk.kid or ""
        kty = jwk.kty or ""
        if kty not in self.ALGORITHM_FOR_KEY_TYPE:
            raise PackRejected(f"key {self.kid!r} is of type {kty!r}, not RSA or EC")
        self.algorithm = self.ALGORITHM_FOR_KEY_TYPE[kty]
        self.thumbprint = jwk.thumbprint()
        try:
            if kty == "RSA":
                self._key: rsa.RSAPublicKey | ec.EllipticCurvePublicKey = rsa.RSAPublicNumbers(
                    e=Base64Url.decode_uint(self._member(jwk, "e"), what="key exponent"),
                    n=Base64Url.decode_uint(self._member(jwk, "n"), what="key modulus"),
                ).public_key()
            else:
                if jwk.crv != "P-256":
                    raise PackRejected(f"key {self.kid!r} is not on P-256")
                self._key = ec.EllipticCurvePublicNumbers(
                    x=Base64Url.decode_uint(self._member(jwk, "x"), what="key x"),
                    y=Base64Url.decode_uint(self._member(jwk, "y"), what="key y"),
                    curve=ec.SECP256R1(),
                ).public_key()
        except ValueError as exc:
            raise PackRejected(f"key {self.kid!r} is not usable: {exc}") from exc

    @classmethod
    def all_in(cls, jwks: Jwks | None) -> list[SigningKey]:
        """The usable keys in a JWKS; malformed entries are skipped."""
        keys: list[SigningKey] = []
        for jwk in jwks.keys if jwks is not None else []:
            try:
                keys.append(cls(jwk))
            except PackRejected:
                continue
        return keys

    @classmethod
    def accepts(cls, algorithm: str | None) -> bool:
        """Whether a header's ``alg`` is one some key type is used with."""
        return algorithm in cls.ALGORITHM_FOR_KEY_TYPE.values()

    def verifies(self, signing_input: bytes, signature: bytes) -> bool:
        try:
            if isinstance(self._key, rsa.RSAPublicKey):
                self._key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            else:
                if len(signature) != 64:
                    return False
                r = int.from_bytes(signature[:32], "big")
                s = int.from_bytes(signature[32:], "big")
                self._key.verify(
                    encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256())
                )
        except InvalidSignature:
            return False
        return True

    def _member(self, jwk: Jwk, name: str) -> str:
        value = getattr(jwk, name)
        if value is None:
            raise PackRejected(f"key {self.kid!r} is missing {name!r}")
        return value
