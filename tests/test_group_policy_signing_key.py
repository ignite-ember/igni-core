"""One of igni server's public keys: which key types are usable and what verifies."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from igni_core.core.config.group_policy.jwks import Jwk, Jwks
from igni_core.core.config.group_policy.jws import PackRejected
from igni_core.core.config.group_policy.signing_key import SigningKey
from tests.group_policy_signing import KEY, ServerKey, _b64, _b64_uint

MESSAGE = b"header.payload"


class EcServerKey:
    """A P-256 signing key, as an ES256 igni server would hold one."""

    def __init__(self) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        numbers = self.private.public_key().public_numbers()
        self.jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64(numbers.x.to_bytes(32, "big")),
            "y": _b64(numbers.y.to_bytes(32, "big")),
            "kid": "ec-1",
        }

    def sign(self, message: bytes = MESSAGE) -> bytes:
        r, s = decode_dss_signature(self.private.sign(message, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


EC = EcServerKey()


def _jwk(**members: object) -> Jwk:
    return Jwk.model_validate(members)


class TestAnRsaKey:
    def test_it_is_rs256_and_carries_the_thumbprint_and_kid(self):
        key = SigningKey(Jwk.model_validate(KEY.jwk))
        assert key.algorithm == "RS256"
        assert key.kid == KEY.kid
        assert key.thumbprint == KEY.thumbprint

    def test_it_verifies_a_pkcs1_sha256_signature_and_nothing_else(self):
        key = SigningKey(Jwk.model_validate(KEY.jwk))
        compact = KEY.sign_claims({"a": 1})
        head, body, sig = compact.split(".")
        raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        signing_input = f"{head}.{body}".encode()

        assert key.verifies(signing_input, raw) is True
        assert key.verifies(signing_input + b"x", raw) is False
        assert key.verifies(signing_input, raw[:-1] + bytes([raw[-1] ^ 1])) is False
        assert key.verifies(signing_input, b"") is False

    def test_another_keys_signature_does_not_verify(self):
        other = ServerKey()
        head, body, sig = other.sign_claims({"a": 1}).split(".")
        raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        assert (
            SigningKey(Jwk.model_validate(KEY.jwk)).verifies(f"{head}.{body}".encode(), raw)
            is False
        )

    def test_a_key_without_a_kid_has_an_empty_one(self):
        jwk = {k: v for k, v in KEY.jwk.items() if k != "kid"}
        assert SigningKey(Jwk.model_validate(jwk)).kid == ""

    @pytest.mark.parametrize("missing", ["n", "e"])
    def test_a_missing_member_is_rejected_by_name(self, missing):
        jwk = {k: v for k, v in KEY.jwk.items() if k != missing}
        with pytest.raises(PackRejected, match=f"missing '{missing}'"):
            SigningKey(Jwk.model_validate(jwk))

    def test_an_exponent_that_is_not_usable_is_rejected_not_raised_raw(self):
        jwk = dict(KEY.jwk, e=_b64_uint(1))
        with pytest.raises(PackRejected, match="not usable"):
            SigningKey(Jwk.model_validate(jwk))


class TestAnEcKey:
    def test_it_is_es256(self):
        key = SigningKey(Jwk.model_validate(EC.jwk))
        assert key.algorithm == "ES256"
        assert key.kid == "ec-1"
        assert key.thumbprint == Jwk.model_validate(EC.jwk).thumbprint()

    def test_it_verifies_a_raw_64_byte_signature(self):
        key = SigningKey(Jwk.model_validate(EC.jwk))
        assert key.verifies(MESSAGE, EC.sign()) is True

    def test_a_tampered_message_or_signature_does_not_verify(self):
        key = SigningKey(Jwk.model_validate(EC.jwk))
        signature = EC.sign()
        assert key.verifies(MESSAGE + b"!", signature) is False
        assert key.verifies(MESSAGE, signature[:-1] + bytes([signature[-1] ^ 1])) is False

    @pytest.mark.parametrize("length", [0, 63, 65, 70, 128])
    def test_a_signature_that_is_not_64_bytes_does_not_verify(self, length):
        key = SigningKey(Jwk.model_validate(EC.jwk))
        signature = EC.sign()
        padded = (signature + b"\x00" * 128)[:length]
        assert key.verifies(MESSAGE, padded) is False

    def test_a_valid_signature_with_a_padding_byte_inside_does_not_verify(self):
        """r and s are each 32 bytes: a zero slipped between them reads as the same
        two numbers, so only the length rule refuses this malleated copy."""
        key = SigningKey(Jwk.model_validate(EC.jwk))
        signature = EC.sign()
        assert key.verifies(MESSAGE, signature) is True
        assert key.verifies(MESSAGE, signature[:32] + b"\x00" + signature[32:]) is False

    def test_a_signature_with_r_and_s_zero_does_not_verify(self):
        key = SigningKey(Jwk.model_validate(EC.jwk))
        assert key.verifies(MESSAGE, bytes(64)) is False

    def test_a_signature_with_r_and_s_out_of_range_does_not_verify(self):
        key = SigningKey(Jwk.model_validate(EC.jwk))
        assert key.verifies(MESSAGE, b"\xff" * 64) is False

    def test_another_keys_signature_does_not_verify(self):
        assert (
            SigningKey(Jwk.model_validate(EC.jwk)).verifies(MESSAGE, EcServerKey().sign()) is False
        )

    @pytest.mark.parametrize("crv", ["P-384", "secp256k1", None, "P-521"])
    def test_a_curve_other_than_p256_is_rejected(self, crv):
        jwk = dict(EC.jwk)
        if crv is None:
            del jwk["crv"]
        else:
            jwk["crv"] = crv
        with pytest.raises(PackRejected, match="not on P-256"):
            SigningKey(Jwk.model_validate(jwk))

    @pytest.mark.parametrize("missing", ["x", "y"])
    def test_a_missing_coordinate_is_rejected_by_name(self, missing):
        jwk = {k: v for k, v in EC.jwk.items() if k != missing}
        with pytest.raises(PackRejected, match=f"missing '{missing}'"):
            SigningKey(Jwk.model_validate(jwk))

    def test_a_point_that_is_not_on_the_curve_is_rejected_not_raised_raw(self):
        jwk = dict(EC.jwk, y=_b64_uint(5))
        with pytest.raises(PackRejected, match="not usable"):
            SigningKey(Jwk.model_validate(jwk))


class TestWhatIsNotAKey:
    @pytest.mark.parametrize("kty", [None, "oct", "OKP", "rsa", ""])
    def test_any_other_key_type_is_rejected_naming_the_key(self, kty):
        members = {"kid": "k9", "n": "AQAB", "e": "AQAB"}
        if kty is not None:
            members["kty"] = kty
        with pytest.raises(PackRejected, match=r"key 'k9' is of type .* not RSA or EC"):
            SigningKey(_jwk(**members))


class TestAccepts:
    @pytest.mark.parametrize("algorithm", ["RS256", "ES256"])
    def test_the_two_algorithms_a_key_type_is_used_with(self, algorithm):
        assert SigningKey.accepts(algorithm) is True

    @pytest.mark.parametrize("algorithm", ["none", "HS256", "RS384", "es256", "", None])
    def test_nothing_else_is(self, algorithm):
        assert SigningKey.accepts(algorithm) is False


class TestAllIn:
    def test_no_key_set_is_no_keys(self):
        assert SigningKey.all_in(None) == []

    def test_an_empty_set_is_no_keys(self):
        assert SigningKey.all_in(Jwks(keys=[])) == []

    def test_an_unusable_key_costs_only_itself(self):
        jwks = Jwks.model_validate(
            {
                "keys": [
                    {"kty": "oct", "kid": "bad-type"},
                    dict(KEY.jwk, n=None) | {"kid": "missing-n"},
                    EC.jwk,
                    {"kty": "EC", "crv": "P-384", "x": "AA", "y": "AA", "kid": "bad-curve"},
                    KEY.jwk,
                ]
            }
        )
        assert [(k.kid, k.algorithm) for k in SigningKey.all_in(jwks)] == [
            ("ec-1", "ES256"),
            (KEY.kid, "RS256"),
        ]
