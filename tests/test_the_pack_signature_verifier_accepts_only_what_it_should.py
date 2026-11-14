"""``PackSignatureVerifier``: a pack is applied only if igni server issued it for this person.

Real cryptography throughout: tokens are signed with real RSA and P-256
keys and checked by the real verifier. Every refusal is a ``PackRejected``
whose message a person can act on.
"""

from __future__ import annotations

import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from igni_core.core.config.group_policy.issuer import IgniServerUrl
from igni_core.core.config.group_policy.jwks import Jwk, Jwks
from igni_core.core.config.group_policy.jws import PackClaims, PackHeader, PackRejected
from igni_core.core.config.group_policy.signature import PackSignatureVerifier, VerifiedPack
from tests.group_policy_signing import ISSUER, KEY, PERSON, ServerKey, _b64, _b64_uint

POLICY = {
    "group_id": "g-1",
    "group_name": "Legal",
    "fetched_at": "2030-01-02T03:04:05Z",
    "entries": [],
}


def _claims(**overrides) -> dict:
    claims = {
        "iss": ISSUER,
        "sub": PERSON,
        "org": "org-1",
        "iat": int(time.time()),
        "version": 5,
        "token_use": PackClaims.USE,
        "policy": POLICY,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not ...}


def _verifier(**overrides) -> PackSignatureVerifier:
    fields = {"issuer": IgniServerUrl(url=ISSUER), "subject": PERSON, "org": "org-1"}
    fields.update(overrides)
    return PackSignatureVerifier(**fields)


def _verify(compact: str, *, jwks: Jwks | None = KEY.key_set, min_version=None, **overrides):
    return _verifier(**overrides).verify(compact, jwks, min_version=min_version)


class _EcKey:
    """A P-256 signing key, as an ES256 igni server would publish."""

    def __init__(self) -> None:
        self._private = ec.generate_private_key(ec.SECP256R1())
        numbers = self._private.public_key().public_numbers()
        jwk = {
            "kty": "EC",
            "crv": "P-256",
            "use": "sig",
            "alg": "ES256",
            "x": _b64_uint(numbers.x).rjust(43, "A"),
            "y": _b64_uint(numbers.y).rjust(43, "A"),
        }
        jwk["kid"] = Jwk.model_validate(jwk).thumbprint()
        self.jwk = jwk

    @property
    def key_set(self) -> Jwks:
        return Jwks.model_validate({"keys": [self.jwk]})

    def sign_claims(self, claims: dict, *, alg: str = "ES256") -> str:
        head = {"alg": alg, "kid": self.jwk["kid"], "typ": PackHeader.TYPE}
        signing_input = f"{_b64(json.dumps(head).encode())}.{_b64(json.dumps(claims).encode())}"
        r, s = decode_dss_signature(
            self._private.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        )
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{signing_input}.{_b64(raw)}"


class TestAGenuinePack:
    def test_it_is_accepted_and_says_what_it_verified(self):
        verified = _verify(KEY.sign_claims(_claims()))

        assert isinstance(verified, VerifiedPack)
        assert verified.policy is not None and verified.policy.group_id == "g-1"
        assert verified.issuer == ISSUER
        assert verified.subject == PERSON
        assert verified.org == "org-1"
        assert verified.version == 5
        assert verified.kid == KEY.kid
        assert verified.pinned is False
        assert verified.absent is False

    def test_an_es256_pack_signed_with_a_p256_key_is_accepted(self):
        key = _EcKey()

        verified = _verify(key.sign_claims(_claims()), jwks=key.key_set)

        assert verified.kid == key.jwk["kid"]

    def test_a_trailing_slash_on_either_issuer_is_ignored(self):
        compact = KEY.sign_claims(_claims(iss=ISSUER + "/"))

        assert _verify(compact).issuer == ISSUER
        assert _verify(KEY.sign_claims(_claims()), issuer=IgniServerUrl(url=ISSUER + "/"))

    def test_an_issuer_in_another_case_of_host_is_the_same_server(self):
        compact = KEY.sign_claims(_claims(iss="https://IGNI.example.invalid"))

        assert _verify(compact).issuer == ISSUER

    def test_a_header_without_a_kid_is_tried_against_every_key_of_its_algorithm(self):
        other = ServerKey()
        jwks = Jwks.model_validate({"keys": [other.jwk, KEY.jwk]})
        compact = KEY.sign_claims(_claims(), header={"kid": None})

        assert _verify(compact, jwks=jwks).kid == ""

    def test_the_key_named_by_kid_is_the_one_used_among_several(self):
        other = ServerKey()
        jwks = Jwks.model_validate({"keys": [other.jwk, KEY.jwk]})

        assert _verify(KEY.sign_claims(_claims()), jwks=jwks).kid == KEY.kid


class TestTheSignature:
    def test_a_payload_changed_after_signing_is_refused(self):
        head, _, signature = KEY.sign_claims(_claims()).split(".")
        forged = _b64(json.dumps(_claims(version=999)).encode())

        with pytest.raises(
            PackRejected, match="signature does not verify against igni server's keys"
        ):
            _verify(f"{head}.{forged}.{signature}")

    def test_a_signature_of_the_wrong_bytes_is_refused(self):
        head, payload, signature = KEY.sign_claims(_claims()).split(".")
        flipped = ("A" if signature[0] != "A" else "B") + signature[1:]

        with pytest.raises(PackRejected, match="signature does not verify"):
            _verify(f"{head}.{payload}.{flipped}")

    def test_a_pack_signed_by_a_key_igni_server_never_held_is_refused(self):
        with pytest.raises(PackRejected, match="signature does not verify"):
            _verify(ServerKey().sign_claims(_claims()))

    @pytest.mark.parametrize("alg", ["HS256", "none", "RS384", "PS256", "ES384", None])
    def test_an_algorithm_other_than_rs256_or_es256_is_refused_by_name(self, alg):
        compact = KEY.sign_claims(_claims(), header={"alg": alg})

        with pytest.raises(PackRejected, match="only RS256 and ES256 are accepted") as raised:
            _verify(compact)

        assert repr(alg) in str(raised.value)

    def test_an_algorithm_that_disagrees_with_the_key_type_is_refused(self):
        compact = KEY.sign_claims(_claims(), header={"alg": "ES256"})

        with pytest.raises(PackRejected, match="signature does not verify"):
            _verify(compact)

    def test_a_kid_that_names_no_key_is_refused(self):
        compact = KEY.sign_claims(_claims(), header={"kid": "someone-else"})

        with pytest.raises(PackRejected, match="signature does not verify"):
            _verify(compact)

    def test_a_valid_signature_under_a_kid_that_belongs_to_another_key_is_refused(self):
        other = ServerKey()
        jwks = Jwks.model_validate({"keys": [KEY.jwk, other.jwk]})
        compact = KEY.sign_claims(_claims(), header={"kid": other.kid})

        with pytest.raises(PackRejected, match="signature does not verify"):
            _verify(compact, jwks=jwks)

    def test_a_crit_header_is_refused(self):
        compact = KEY.sign_claims(_claims(), header={"crit": ["exp"]})

        with pytest.raises(PackRejected, match="critical header"):
            _verify(compact)

    def test_a_jws_of_another_type_is_refused(self):
        compact = KEY.sign_claims(_claims(), header={"typ": "JWT"})

        with pytest.raises(PackRejected, match="not a group pack"):
            _verify(compact)

    @pytest.mark.parametrize("text", ["", "abc", "a.b", "a.b.c.d", None, 7])
    def test_something_that_is_not_a_compact_jws_is_refused(self, text):
        with pytest.raises(PackRejected, match="not a compact JWS"):
            _verify(text)


class TestWhichKeysAreUsed:
    def test_no_key_set_at_all_refuses(self):
        with pytest.raises(PackRejected, match="no igni server key to check the pack against"):
            _verify(KEY.sign_claims(_claims()), jwks=None)

    def test_an_empty_key_set_refuses(self):
        with pytest.raises(PackRejected, match="no igni server key"):
            _verify(KEY.sign_claims(_claims()), jwks=Jwks(keys=[]))

    def test_a_pinned_key_is_accepted_and_the_result_says_it_was_pinned(self):
        verified = _verify(KEY.sign_claims(_claims()), pins=(KEY.thumbprint,))

        assert verified.pinned is True

    def test_a_pin_with_no_key_set_refuses_with_the_pin_message(self):
        with pytest.raises(PackRejected, match="pinned in group_policy.trusted_keys is available"):
            _verify(KEY.sign_claims(_claims()), jwks=None, pins=(KEY.thumbprint,))

    def test_a_key_set_that_does_not_hold_the_pinned_key_refuses(self):
        other = ServerKey()

        with pytest.raises(PackRejected, match="pinned in group_policy.trusted_keys is available"):
            _verify(KEY.sign_claims(_claims()), jwks=other.key_set, pins=(KEY.thumbprint,))

    def test_a_cached_key_set_swapped_under_a_pin_cannot_introduce_a_key(self):
        forger = ServerKey()
        compact = forger.sign_claims(_claims())

        with pytest.raises(PackRejected, match="pinned in group_policy.trusted_keys is available"):
            _verify(compact, jwks=forger.key_set, pins=(KEY.thumbprint,))

    def test_with_a_pin_a_signature_that_fails_says_a_pinned_key(self):
        jwks = Jwks.model_validate({"keys": [KEY.jwk]})
        head, payload, signature = ServerKey().sign_claims(_claims()).split(".")

        with pytest.raises(PackRejected, match="does not verify against a pinned key"):
            _verify(f"{head}.{payload}.{signature}", jwks=jwks, pins=(KEY.thumbprint,))

    def test_the_pin_is_the_computed_thumbprint_not_the_kid(self):
        jwk = dict(KEY.jwk, kid="a-label-the-server-chose")
        jwks = Jwks.model_validate({"keys": [jwk]})
        compact = KEY.sign_claims(_claims(), header={"kid": "a-label-the-server-chose"})

        assert _verify(compact, jwks=jwks, pins=(KEY.thumbprint,)).pinned is True
        with pytest.raises(PackRejected, match="pinned in group_policy.trusted_keys"):
            _verify(compact, jwks=jwks, pins=("a-label-the-server-chose",))


class TestWhoTheyAreFor:
    def test_a_token_that_is_not_a_group_pack_is_refused(self):
        with pytest.raises(PackRejected, match="not a group pack"):
            _verify(KEY.sign_claims(_claims(token_use="access")))

    def test_a_token_with_no_use_is_refused(self):
        with pytest.raises(PackRejected, match="not a group pack"):
            _verify(KEY.sign_claims(_claims(token_use=...)))

    def test_with_no_api_url_the_issuer_cannot_be_checked(self):
        with pytest.raises(PackRejected, match="api_url is not set"):
            _verify(KEY.sign_claims(_claims()), issuer=IgniServerUrl(url=None))

    def test_another_server_is_refused_and_both_are_named(self):
        compact = KEY.sign_claims(_claims(iss="https://elsewhere.example.invalid"))

        with pytest.raises(PackRejected) as raised:
            _verify(compact)

        assert "https://elsewhere.example.invalid" in str(raised.value)
        assert ISSUER in str(raised.value)

    def test_no_issuer_claim_is_refused(self):
        with pytest.raises(PackRejected, match="was issued by None"):
            _verify(KEY.sign_claims(_claims(iss=...)))

    def test_nobody_signed_in_is_refused(self):
        with pytest.raises(PackRejected, match="nobody is signed in"):
            _verify(KEY.sign_claims(_claims()), subject=None)

    def test_another_person_is_refused(self):
        with pytest.raises(PackRejected, match="somebody other than the person signed in"):
            _verify(KEY.sign_claims(_claims(sub="person-2")))

    def test_no_subject_claim_is_refused(self):
        with pytest.raises(PackRejected, match="somebody other than the person signed in"):
            _verify(KEY.sign_claims(_claims(sub=...)))

    def test_another_organisation_is_refused_and_both_are_named(self):
        with pytest.raises(PackRejected, match="'org-2'.*'org-1'"):
            _verify(KEY.sign_claims(_claims(org="org-2")))

    @pytest.mark.parametrize(
        ("pack_org", "session_org", "expected"),
        [
            (None, "org-1", None),
            ("", "org-1", None),
            ("org-9", None, "org-9"),
            ("org-9", "", "org-9"),
        ],
    )
    def test_an_organisation_missing_on_either_side_does_not_constrain(
        self, pack_org, session_org, expected
    ):
        verified = _verify(KEY.sign_claims(_claims(org=pack_org)), org=session_org)

        assert verified.org == expected


class TestTheVersion:
    def test_a_pack_with_no_version_is_refused(self):
        with pytest.raises(PackRejected, match="carries no version"):
            _verify(KEY.sign_claims(_claims(version=...)))

    def test_a_version_that_is_not_an_integer_is_refused(self):
        with pytest.raises(PackRejected, match="malformed"):
            _verify(KEY.sign_claims(_claims(version="5")))

    def test_an_older_pack_than_the_newest_accepted_is_a_rollback(self):
        with pytest.raises(
            PackRejected, match=r"version 5\).*\(version 6\); a rollback is refused"
        ):
            _verify(KEY.sign_claims(_claims(version=5)), min_version=6)

    def test_the_same_version_is_not_a_rollback_and_a_newer_one_is_fine(self):
        assert _verify(KEY.sign_claims(_claims(version=5)), min_version=5).version == 5
        assert _verify(KEY.sign_claims(_claims(version=9)), min_version=5).version == 9

    def test_a_version_of_zero_is_a_version(self):
        assert _verify(KEY.sign_claims(_claims(version=0))).version == 0


class TestThePolicy:
    def test_a_signed_absence_is_accepted_with_no_policy(self):
        verified = _verify(KEY.sign_absence())

        assert verified.absent is True
        assert verified.policy is None

    def test_a_missing_policy_that_is_not_an_absence_is_refused(self):
        with pytest.raises(PackRejected, match="carries no policy"):
            _verify(KEY.sign_claims(_claims(policy=None)))

    def test_no_pack_set_to_something_other_than_true_is_not_an_absence(self):
        with pytest.raises(PackRejected, match="carries no policy"):
            _verify(KEY.sign_claims(_claims(policy=None, no_pack="true")))

    def test_an_absence_beside_a_policy_is_still_an_absence(self):
        verified = _verify(KEY.sign_claims(_claims(no_pack=True)))

        assert verified.absent is True and verified.policy is None

    def test_a_policy_that_does_not_parse_is_refused_naming_the_cause(self):
        with pytest.raises(PackRejected, match="signed pack does not parse"):
            _verify(KEY.sign_claims(_claims(policy={"entries": [{"kind": "agents"}]})))

    def test_a_policy_that_is_not_an_object_is_refused(self):
        with pytest.raises(PackRejected, match="malformed"):
            _verify(KEY.sign_claims(_claims(policy=[1, 2])))

    def test_the_policy_is_read_leniently(self):
        verified = _verify(KEY.sign_claims(_claims(policy={"group_id": 7, "entries": None})))

        assert verified.policy is not None
        assert verified.policy.group_id == "7"
        assert verified.policy.entries == []
