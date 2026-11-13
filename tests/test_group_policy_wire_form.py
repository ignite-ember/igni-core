"""A pack's wire form, its issuer and the answers a request for it can give.

The compact JWS is what igni server sends and the issuer is where its keys
are fetched from, so a crafted value of either must be refused with a reason
and never taken for a pack, or trusted over a network that could swap it.
"""

from __future__ import annotations

import base64
import json

import pytest
from pydantic import ValidationError

from igni_core.core.config.group_policy.answers import (
    PACK_UNCHANGED,
    SignedAbsence,
    UnchangedPack,
)
from igni_core.core.config.group_policy.issuer import IgniServerUrl
from igni_core.core.config.group_policy.jws import (
    Base64Url,
    CompactPack,
    PackClaims,
    PackHeader,
    PackRejected,
)


def _seg(value: object) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _compact(header: object, claims: object, signature: bytes = b"sig") -> str:
    return f"{_seg(header)}.{_seg(claims)}.{_seg(signature)}"


GOOD_HEADER = {"alg": "ES256", "typ": "igni-group-policy+jwt", "kid": "k"}


class TestBase64Url:
    def test_unpadded_input_decodes(self):
        assert Base64Url.decode("aGk", what="x") == b"hi"
        assert Base64Url.decode("-_8", what="x") == b"\xfb\xff"

    def test_padding_already_present_is_accepted(self):
        assert Base64Url.decode("aGk=", what="x") == b"hi"

    def test_a_length_that_cannot_be_base64_is_refused_naming_the_part(self):
        with pytest.raises(PackRejected, match="the pack's signature is not base64url"):
            Base64Url.decode("a", what="signature")

    def test_a_non_ascii_segment_is_refused(self):
        with pytest.raises(PackRejected, match="not base64url"):
            Base64Url.decode("é", what="header")

    def test_an_unsigned_integer_is_big_endian(self):
        assert Base64Url.decode_uint(_seg(b"\x01\x00"), what="n") == 256


class TestCompactPack:
    @pytest.mark.parametrize("text", ["", "abc", "a.b", "a.b.c.d", None, 7, b"a.b.c"])
    def test_anything_but_two_dots_is_unsigned_or_truncated(self, text: object):
        with pytest.raises(PackRejected, match="not a compact JWS"):
            CompactPack.parse(text)

    def test_surrounding_whitespace_is_stripped(self):
        pack = CompactPack.parse("  h.p.s \n")
        assert (pack.header_segment, pack.payload_segment, pack.signature_segment) == ("h", "p", "s")

    def test_the_signing_input_is_header_dot_payload(self):
        pack = CompactPack.parse(_compact(GOOD_HEADER, {"sub": "p"}))
        assert pack.signing_input() == f"{pack.header_segment}.{pack.payload_segment}".encode()

    def test_a_non_ascii_signing_input_is_refused(self):
        with pytest.raises(PackRejected, match="not base64url"):
            CompactPack.parse("é.p.s").signing_input()

    def test_the_signature_is_the_decoded_bytes(self):
        assert CompactPack.parse(_compact(GOOD_HEADER, {}, b"\x00\x01")).signature() == b"\x00\x01"

    def test_the_header_and_claims_are_read_from_their_segments(self):
        pack = CompactPack.parse(_compact(GOOD_HEADER, {"sub": "p", "version": 3}))
        assert pack.header().kid == "k"
        assert pack.claims().version == 3

    def test_a_payload_that_is_not_json_is_refused(self):
        text = f"{_seg(GOOD_HEADER)}.{_seg(b'not json')}.{_seg(b's')}"
        with pytest.raises(PackRejected, match="payload is not JSON"):
            CompactPack.parse(text).claims()

    def test_a_payload_that_is_a_list_is_refused(self):
        with pytest.raises(PackRejected, match="payload is not an object"):
            CompactPack.parse(_compact(GOOD_HEADER, [1])).claims()

    def test_a_header_that_is_not_json_is_refused(self):
        with pytest.raises(PackRejected, match="header is not JSON"):
            CompactPack.parse(f"{_seg(b'{')}.{_seg({})}.{_seg(b's')}").header()

    def test_a_claim_of_the_wrong_type_is_a_malformed_payload(self):
        with pytest.raises(PackRejected, match="payload is malformed"):
            CompactPack.parse(_compact(GOOD_HEADER, {"version": "3"})).claims()

    def test_unverified_claims_are_the_claims(self):
        claims = CompactPack.unverified_claims(_compact(GOOD_HEADER, {"org": 12}))
        assert claims is not None and claims.org == "12"

    @pytest.mark.parametrize("text", ["junk", "a.b.c", _compact(GOOD_HEADER, [1])])
    def test_unverified_claims_never_raise(self, text: str):
        assert CompactPack.unverified_claims(text) is None


class TestPackHeader:
    def test_a_group_pack_header_passes(self):
        PackHeader.model_validate(GOOD_HEADER).check()

    def test_a_critical_parameter_is_refused(self):
        with pytest.raises(PackRejected, match="critical header"):
            PackHeader.model_validate({**GOOD_HEADER, "crit": ["exp"]}).check()

    def test_an_empty_crit_is_not_critical(self):
        PackHeader.model_validate({**GOOD_HEADER, "crit": []}).check()

    @pytest.mark.parametrize("typ", [None, "JWT", "igni-group-policy+JWT"])
    def test_a_header_that_is_not_a_group_pack_is_refused(self, typ: str | None):
        with pytest.raises(PackRejected, match="not a group pack"):
            PackHeader.model_validate({"alg": "ES256", "typ": typ}).check()

    def test_unknown_header_members_are_ignored(self):
        assert PackHeader.model_validate({**GOOD_HEADER, "jku": "http://x"}).kid == "k"


class TestPackClaims:
    def test_numbers_are_read_as_text(self):
        claims = PackClaims.model_validate({"sub": 5, "org": 6, "iss": 7})
        assert (claims.sub, claims.org, claims.iss) == ("5", "6", "7")

    @pytest.mark.parametrize("version", ["1", 1.5, True])
    def test_a_version_that_is_not_an_integer_is_refused(self, version: object):
        with pytest.raises(ValidationError):
            PackClaims.model_validate({"version": version})

    def test_unknown_claims_are_ignored(self):
        assert PackClaims.model_validate({"surprise": 1}).token_use is None

    def test_only_a_true_no_pack_is_an_absence(self):
        assert PackClaims.model_validate({"no_pack": True}).absent
        for value in ("true", 1, None, False):
            assert not PackClaims.model_validate({"no_pack": value}).absent

    def test_claims_are_frozen(self):
        with pytest.raises(ValidationError):
            PackClaims.model_validate({}).sub = "x"  # type: ignore[misc]


class TestIgniServerUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (None, ""),
            ("", ""),
            ("https://Igni.Example.COM/", "https://igni.example.com"),
            ("HTTPS://host:8443/api/", "https://host:8443/api"),
            ("  https://host/a/b//  ", "https://host/a/b"),
            ("host.example/", "host.example"),
            ("  garbage/ ", "garbage"),
        ],
    )
    def test_it_is_scheme_host_port_path_without_a_trailing_slash(
        self, url: str | None, expected: str
    ):
        assert IgniServerUrl(url=url).normalised == expected

    def test_the_keys_live_at_the_well_known_path(self):
        url = IgniServerUrl(url="https://Igni.example/api/")
        assert url.jwks_url() == "https://igni.example/api/.well-known/jwks.json"

    @pytest.mark.parametrize(
        "url",
        [
            "https://igni.example",
            "http://localhost:8000",
            "http://127.0.0.1",
            "http://[::1]:9000/x",
            "http://LOCALHOST",
        ],
    )
    def test_keys_are_trusted_over_tls_and_over_plain_http_to_loopback(self, url: str):
        assert IgniServerUrl(url=url).may_trust_unpinned_jwks()

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "igni.example",
            "http://igni.example",
            "http://10.0.0.5",
            "http://localhost.evil.example",
            "ftp://localhost",
            "http://localhost@evil.example",
        ],
    )
    def test_keys_are_not_trusted_unpinned_anywhere_else(self, url: str | None):
        assert not IgniServerUrl(url=url).may_trust_unpinned_jwks()


class TestAnswers:
    def test_an_unchanged_answer_is_its_own_kind_and_never_none(self):
        assert PACK_UNCHANGED is not None
        assert PACK_UNCHANGED.kind == "unchanged"
        assert PACK_UNCHANGED == UnchangedPack()

    def test_an_unchanged_answer_is_frozen(self):
        with pytest.raises(ValidationError):
            PACK_UNCHANGED.kind = "other"  # type: ignore[misc]

    def test_a_signed_absence_carries_its_jws_and_optionally_a_tag(self):
        assert SignedAbsence(signed_pack="a.b.c").etag is None
        assert SignedAbsence(signed_pack="a.b.c", etag='"v1"').etag == '"v1"'

    def test_a_signed_absence_is_frozen_and_is_not_an_unchanged_answer(self):
        absence = SignedAbsence(signed_pack="a.b.c")
        with pytest.raises(ValidationError):
            absence.signed_pack = "x"  # type: ignore[misc]
        assert absence != PACK_UNCHANGED
