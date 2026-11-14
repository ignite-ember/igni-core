"""igni server's published keys: their thumbprints, and a bad entry costing only itself."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from igni_core.core.config.group_policy.jwks import Jwk, Jwks
from igni_core.core.config.group_policy.jws import PackRejected

#: The example key of RFC 7638 section 3.1, and the thumbprint the RFC gives for it.
RFC_7638_N = (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPebWKRX"
    "jBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaS"
    "qzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksIN"
    "HaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
)
RFC_7638_THUMBPRINT = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


class TestThumbprint:
    def test_an_rsa_key_has_the_thumbprint_the_rfc_gives(self):
        key = Jwk(kty="RSA", n=RFC_7638_N, e="AQAB", kid="ignored", alg="RS256", use="sig")
        assert key.thumbprint() == RFC_7638_THUMBPRINT

    def test_an_ec_key_is_hashed_over_crv_kty_x_y_in_that_order(self):
        key = Jwk(kty="EC", crv="P-256", x="xx", y="yy", kid="not-the-thumbprint")
        canonical = b'{"crv":"P-256","kty":"EC","x":"xx","y":"yy"}'
        digest = hashlib.sha256(canonical).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert key.thumbprint() == expected

    @pytest.mark.parametrize("kty", [None, "oct", "OKP"])
    def test_any_other_key_type_is_rejected(self, kty: str | None):
        with pytest.raises(PackRejected, match="not RSA or EC"):
            Jwk(kty=kty, x="x").thumbprint()


class TestParse:
    @pytest.mark.parametrize("raw", [[], "keys", None, 7])
    def test_what_is_not_an_object_is_not_a_jwks(self, raw: object):
        assert Jwks.parse(raw) is None

    @pytest.mark.parametrize("keys", [None, "k", {"kty": "RSA"}, 3])
    def test_keys_that_are_not_a_list_are_an_empty_set(self, keys: object):
        parsed = Jwks.parse({"keys": keys})
        assert parsed is not None and parsed.keys == []

    def test_no_keys_member_is_an_empty_set(self):
        parsed = Jwks.parse({})
        assert parsed is not None and parsed.keys == []

    def test_an_entry_that_is_not_an_object_costs_only_itself(self):
        parsed = Jwks.parse({"keys": ["junk", 4, None, {"kty": "RSA", "kid": "good"}]})
        assert parsed is not None
        assert [k.kid for k in parsed.keys] == ["good"]

    def test_a_key_whose_members_have_the_wrong_type_costs_only_itself(self):
        parsed = Jwks.parse(
            {"keys": [{"kty": 5, "kid": "bad"}, {"kty": "EC", "kid": "good", "crv": "P-256"}]}
        )
        assert parsed is not None
        assert [k.kid for k in parsed.keys] == ["good"]


class TestAsMapping:
    def test_it_is_what_jwks_json_holds_without_empty_members_and_with_unknown_ones(self):
        jwks = Jwks.parse({"keys": [{"kty": "EC", "kid": "k", "crv": "P-256", "x5t": "extra"}]})
        assert jwks is not None
        mapping = jwks.as_mapping()
        assert mapping == {"keys": [{"kty": "EC", "kid": "k", "crv": "P-256", "x5t": "extra"}]}
        assert json.loads(json.dumps(mapping)) == mapping
