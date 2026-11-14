"""A publisher's keys and a compact JWS, at their edges.

Whatever arrives beside a bundle is untrusted: a refusal is always a
``BundleSignatureError`` with a message for the person, never another
exception, and never a bundle that was not signed by the publisher.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from igni_core.extensions.bundle_signature import (
    BundleSignatureError,
    PublisherKey,
    PublisherKeys,
)
from tests.extension_distribution_helpers import Publisher, b64url

CLAIM = {"extension": "acme.notes", "version": "1.0.0", "sha256": "ab" * 32}


@pytest.fixture(scope="module")
def rsa_publisher() -> Publisher:
    return Publisher("RSA", kid="rsa-key")


@pytest.fixture(scope="module")
def ec_publisher() -> Publisher:
    return Publisher("EC", kid="ec-key")


def _token(publisher: Publisher, payload_segment: str | None = None, **replace: str) -> str:
    head, payload, signature = publisher.sign(CLAIM).split(".")
    return ".".join(
        [
            replace.get("head", head),
            payload_segment if payload_segment is not None else payload,
            replace.get("signature", signature),
        ]
    )


# ── PublisherKey ───────────────────────────────────────────────────


@pytest.mark.parametrize("kty", ["oct", "OKP", "", None])
def test_a_key_that_is_neither_rsa_nor_ec_is_refused(kty):
    with pytest.raises(BundleSignatureError, match="is not RSA or EC"):
        PublisherKey({"kty": kty, "kid": "k"})


def test_a_key_declaring_another_algorithm_is_refused(rsa_publisher, ec_publisher):
    with pytest.raises(BundleSignatureError, match="declares 'ES256'"):
        PublisherKey({**rsa_publisher.jwk(), "alg": "ES256"})
    with pytest.raises(BundleSignatureError, match="declares 'RS256'"):
        PublisherKey({**ec_publisher.jwk(), "alg": "RS256"})


def test_a_key_declaring_its_own_algorithm_is_taken(rsa_publisher, ec_publisher):
    assert PublisherKey({**rsa_publisher.jwk(), "alg": "RS256"}).algorithm == "RS256"
    assert PublisherKey({**ec_publisher.jwk(), "alg": "ES256"}).algorithm == "ES256"


@pytest.mark.parametrize("missing", ["n", "e"])
def test_an_rsa_key_missing_a_member_is_refused_naming_it(rsa_publisher, missing):
    jwk = {k: v for k, v in rsa_publisher.jwk().items() if k != missing}
    with pytest.raises(BundleSignatureError, match=f"is missing '{missing}'"):
        PublisherKey(jwk)


@pytest.mark.parametrize("missing", ["x", "y"])
def test_an_ec_key_missing_a_member_is_refused_naming_it(ec_publisher, missing):
    jwk = {k: v for k, v in ec_publisher.jwk().items() if k != missing}
    with pytest.raises(BundleSignatureError, match=f"is missing '{missing}'"):
        PublisherKey(jwk)


@pytest.mark.parametrize("curve", ["P-384", "secp256k1", None])
def test_an_ec_key_on_another_curve_is_refused(ec_publisher, curve):
    with pytest.raises(BundleSignatureError, match="is not on P-256"):
        PublisherKey({**ec_publisher.jwk(), "crv": curve})


def test_an_ec_point_off_the_curve_is_refused(ec_publisher):
    with pytest.raises(BundleSignatureError, match="is not usable"):
        PublisherKey({**ec_publisher.jwk(), "y": b64url((1).to_bytes(32, "big"))})


def test_an_rsa_key_with_unusable_numbers_is_refused(rsa_publisher):
    with pytest.raises(BundleSignatureError, match="is not usable"):
        PublisherKey({**rsa_publisher.jwk(), "e": b64url(b"\x00")})


def test_a_key_member_that_is_not_base64url_is_refused(rsa_publisher):
    with pytest.raises(BundleSignatureError, match="key modulus is not base64url"):
        PublisherKey({**rsa_publisher.jwk(), "n": "a"})


def test_a_key_without_a_kid_has_an_empty_one(rsa_publisher):
    jwk = {k: v for k, v in rsa_publisher.jwk().items() if k != "kid"}
    assert PublisherKey(jwk).kid == ""


def test_a_key_verifies_only_what_it_signed(rsa_publisher, ec_publisher):
    for publisher in (rsa_publisher, ec_publisher):
        token = publisher.sign(CLAIM)
        head, payload, signature = token.split(".")
        raw = PublisherKey(publisher.jwk())
        sig = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        signing_input = f"{head}.{payload}".encode()
        assert raw.verifies(signing_input, sig) is True
        assert raw.verifies(signing_input + b"x", sig) is False
        assert raw.verifies(signing_input, b"\x00" * len(sig)) is False


@pytest.mark.parametrize("length", [0, 63, 65])
def test_an_es256_signature_of_the_wrong_length_is_false_not_an_error(ec_publisher, length):
    assert PublisherKey(ec_publisher.jwk()).verifies(b"input", b"\x01" * length) is False


# ── PublisherKeys ──────────────────────────────────────────────────


@pytest.mark.parametrize("jwks", [{}, {"keys": None}, {"keys": "abc"}, {"keys": {}}, {"keys": []}])
def test_a_key_set_without_a_list_of_keys_is_refused(jwks):
    with pytest.raises(BundleSignatureError, match="no keys"):
        PublisherKeys(jwks)


def test_entries_that_are_not_objects_are_ignored(rsa_publisher):
    keys = PublisherKeys({"keys": ["junk", 3, None, rsa_publisher.jwk()]})
    assert keys.verify(rsa_publisher.sign(CLAIM)).extension == "acme.notes"


def test_a_key_set_of_only_junk_verifies_nothing(rsa_publisher):
    with pytest.raises(BundleSignatureError, match="not signed by its publisher"):
        PublisherKeys({"keys": ["junk", 3]}).verify(rsa_publisher.sign(CLAIM))


def test_a_bad_key_in_the_set_refuses_the_set(rsa_publisher):
    with pytest.raises(BundleSignatureError, match="is not RSA or EC"):
        PublisherKeys({"keys": [rsa_publisher.jwk(), {"kty": "oct"}]})


def test_a_kid_narrows_to_that_key_even_when_another_would_verify(rsa_publisher):
    twin = {**rsa_publisher.jwk(), "kid": "twin"}
    keys = PublisherKeys({"keys": [twin]})
    token = rsa_publisher.sign(CLAIM, header={"alg": "RS256", "kid": "rsa-key"})
    with pytest.raises(BundleSignatureError, match="not signed by its publisher"):
        keys.verify(token)


def test_a_kid_of_a_key_with_another_algorithm_is_not_tried(rsa_publisher, ec_publisher):
    keys = PublisherKeys({"keys": [rsa_publisher.jwk(), {**ec_publisher.jwk(), "kid": "rsa-key"}]})
    assert keys.verify(rsa_publisher.sign(CLAIM)).sha256 == CLAIM["sha256"]


def test_without_a_kid_every_key_of_the_algorithm_is_tried(rsa_publisher):
    other = Publisher("RSA", kid="other")
    keys = PublisherKeys({"keys": [other.jwk(), rsa_publisher.jwk()]})
    token = rsa_publisher.sign(CLAIM, header={"alg": "RS256"})
    assert keys.verify(token).version == "1.0.0"


def test_surrounding_whitespace_is_ignored(rsa_publisher):
    keys = PublisherKeys(rsa_publisher.jwks())
    assert keys.verify(f"  {rsa_publisher.sign(CLAIM)}\n").extension == "acme.notes"


@pytest.mark.parametrize("shape", ["a.b", "a.b.c.d", "abc", ""])
def test_a_token_that_is_not_three_segments_is_refused(rsa_publisher, shape):
    with pytest.raises(BundleSignatureError, match="not a compact JWS"):
        PublisherKeys(rsa_publisher.jwks()).verify(shape)


def test_a_segment_that_is_not_base64url_is_refused(rsa_publisher):
    keys = PublisherKeys(rsa_publisher.jwks())
    with pytest.raises(BundleSignatureError, match="header is not base64url"):
        keys.verify(_token(rsa_publisher, head="a"))
    with pytest.raises(BundleSignatureError, match="signature is not base64url"):
        keys.verify(_token(rsa_publisher, signature="a"))
    with pytest.raises(BundleSignatureError, match="payload is not base64url"):
        keys.verify(_sign_raw(rsa_publisher, _token(rsa_publisher).split(".")[0], "a"))


@pytest.mark.parametrize(
    "raw", [b"not json", b"[1, 2]", b'"RS256"', b"\xff\xfe\xfd"], ids=["text", "list", "str", "bin"]
)
def test_a_header_that_is_not_a_json_object_is_refused(rsa_publisher, raw):
    token = _token(rsa_publisher, head=b64url(raw))
    with pytest.raises(BundleSignatureError, match="header is not"):
        PublisherKeys(rsa_publisher.jwks()).verify(token)


@pytest.mark.parametrize("crit", [["exp"], "exp", {"a": 1}])
def test_a_critical_header_is_refused(rsa_publisher, crit):
    token = rsa_publisher.sign(CLAIM, header={"alg": "RS256", "kid": "rsa-key", "crit": crit})
    with pytest.raises(BundleSignatureError, match="critical header"):
        PublisherKeys(rsa_publisher.jwks()).verify(token)


@pytest.mark.parametrize("header", [{}, {"alg": None}, {"alg": ["RS256"]}])
def test_a_header_without_a_usable_algorithm_is_refused(rsa_publisher, header):
    with pytest.raises(BundleSignatureError, match="only RS256 and ES256"):
        PublisherKeys(rsa_publisher.jwks()).verify(rsa_publisher.sign(CLAIM, header=header))


def test_a_token_with_a_character_beyond_ascii_is_refused_not_crashed(rsa_publisher):
    keys = PublisherKeys(rsa_publisher.jwks())
    for token in (
        _token(rsa_publisher, "é"),
        _token(rsa_publisher, head="é"),
        _token(rsa_publisher, signature="é"),
    ):
        with pytest.raises(BundleSignatureError):
            keys.verify(token)


@pytest.mark.parametrize(
    "payload",
    [
        {"extension": "acme.notes", "version": "1.0.0"},
        {**CLAIM, "version": 1},
        ["not", "an", "object"],
    ],
    ids=["missing", "non-string", "list"],
)
def test_a_signed_claim_that_is_not_exactly_the_three_strings_is_refused(rsa_publisher, payload):
    token = rsa_publisher.sign(payload)
    with pytest.raises(BundleSignatureError, match="claim is malformed"):
        PublisherKeys(rsa_publisher.jwks()).verify(token)


def test_a_signed_payload_that_is_not_json_is_refused(rsa_publisher):
    keys = PublisherKeys(rsa_publisher.jwks())
    head = b64url(json.dumps({"alg": "RS256", "kid": "rsa-key"}).encode())
    for raw in (b"not json", b"\xff\xfe"):
        # Sign these exact bytes with the publisher's key.
        signed = _sign_raw(rsa_publisher, head, b64url(raw))
        with pytest.raises(BundleSignatureError, match="claim is malformed"):
            keys.verify(signed)


def _sign_raw(publisher: Publisher, head: str, payload: str) -> str:
    signing_input = f"{head}.{payload}".encode()
    signature = publisher._private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{payload}.{b64url(signature)}"
