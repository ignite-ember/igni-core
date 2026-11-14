"""The auth wire shapes: what they default, ignore, refuse, and how a JWT is read."""

from __future__ import annotations

import base64
import json

import pytest
from pydantic import ValidationError

from igni_core.core.auth.credentials import Credentials
from igni_core.core.auth.schemas import (
    JwtClaims,
    LoadCredentialsResult,
    LoginResult,
    SignedInSession,
    UserInfo,
    ValidateResult,
)


def _jwt(payload: object, *, raw: bytes | None = None, strip: bool = True) -> str:
    body = raw if raw is not None else json.dumps(payload).encode()
    seg = base64.urlsafe_b64encode(body).decode()
    if strip:
        seg = seg.rstrip("=")
    return f"h.{seg}.sig"


class TestJwtClaimsDecode:
    def test_a_claim_of_the_wrong_type_gives_none_not_a_raw_validation_error(self):
        assert JwtClaims.decode(_jwt({"sub": "u", "exp": "soon"})) is None
        assert JwtClaims.decode(_jwt({"sub": 7})) is None

    def test_every_padding_remainder_decodes(self):
        remainders = set()
        for n in range(1, 8):
            payload = {"sub": "x" * n}
            token = _jwt(payload)
            remainders.add(len(token.split(".")[1]) % 4)
            claims = JwtClaims.decode(token)
            assert claims is not None
            assert claims.sub == "x" * n
        # Unpadded base64 leaves 0, 2 or 3 characters over (never 1).
        assert remainders == {0, 2, 3}

    def test_the_url_safe_alphabet_is_read(self):
        # Bytes chosen so the standard alphabet would use + and /.
        raw = json.dumps({"org_name": "ÿþý??>>"}).encode()
        seg = base64.urlsafe_b64encode(raw).decode()
        assert "-" in seg or "_" in seg
        claims = JwtClaims.decode(f"h.{seg}.s")
        assert claims is not None
        assert claims.org_name == "ÿþý??>>"

    @pytest.mark.parametrize("payload", [["a"], "text", 5, None])
    def test_a_payload_that_is_not_an_object_gives_none(self, payload):
        assert JwtClaims.decode(_jwt(payload)) is None

    def test_a_payload_that_is_not_json_gives_none(self):
        assert JwtClaims.decode(_jwt(None, raw=b"not json")) is None
        assert JwtClaims.decode(_jwt(None, raw=b"\xff\xfe\x00")) is None

    @pytest.mark.parametrize("token", ["", "abc", "a.!!!!.c", "a..c"])
    def test_a_malformed_token_gives_none(self, token):
        assert JwtClaims.decode(token) is None

    def test_the_identity_claims_are_read_and_unknown_ones_ignored(self):
        claims = JwtClaims.decode(
            _jwt(
                {
                    "sub": "s1",
                    "kind": "space",
                    "app": "a1",
                    "external_id": "ext",
                    "email": "ü@example.com",
                    "exp": 0,
                    "org": "o1",
                    "org_name": "Org Ü",
                    "group": "g1",
                    "surprise": [1],
                }
            )
        )
        assert claims is not None
        assert claims.model_dump() == {
            "sub": "s1",
            "kind": "space",
            "app": "a1",
            "external_id": "ext",
            "email": "ü@example.com",
            "exp": 0,
            "org": "o1",
            "org_name": "Org Ü",
            "group": "g1",
        }

    def test_an_empty_object_is_all_none(self):
        assert JwtClaims.decode(_jwt({})) == JwtClaims()


class TestUserInfo:
    def test_defaults(self):
        user = UserInfo()
        assert (user.email, user.full_name, user.tier, user.org_display_name) == (
            "",
            None,
            None,
            None,
        )

    def test_a_name_is_kept_and_unknown_fields_ignored(self):
        user = UserInfo.model_validate(
            {"email": "a@b.c", "full_name": "Ada L", "org_display_name": "Acme", "x": 1}
        )
        assert (user.email, user.full_name, user.org_display_name) == ("a@b.c", "Ada L", "Acme")
        assert not hasattr(user, "x")


class TestResultReasons:
    def test_unknown_reasons_are_refused(self):
        with pytest.raises(ValidationError):
            LoginResult(ok=False, reason="nope")
        with pytest.raises(ValidationError):
            ValidateResult(ok=False, reason="nope")
        with pytest.raises(ValidationError):
            LoadCredentialsResult(ok=False, reason="nope")

    def test_known_reasons_and_defaults(self):
        for reason in ("ok", "timeout", "port_bind_failed", "handler_error", "error"):
            assert LoginResult(ok=False, reason=reason).reason == reason
        for reason in ("ok", "http_error", "network_error", "decode_error", "schema_mismatch"):
            assert ValidateResult(ok=False, reason=reason).reason == reason
        for reason in ("ok", "no_file", "malformed_json", "schema_mismatch", "error"):
            assert LoadCredentialsResult(ok=False, reason=reason).reason == reason
        assert LoginResult(ok=True, token="t", extra_field=1).token == "t"

    def test_a_load_result_holds_real_credentials(self):
        creds = Credentials(access_token="t")
        result = LoadCredentialsResult(ok=True, creds=creds)
        assert result.creds == creds


class TestSignedInSession:
    def test_nobody_signed_in_is_all_none(self):
        s = SignedInSession()
        assert (s.subject, s.org, s.token) == (None, None, None)

    def test_it_is_frozen(self):
        s = SignedInSession(subject="u", org="o", token="t")
        with pytest.raises(ValidationError):
            s.subject = "somebody else"
