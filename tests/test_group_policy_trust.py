"""What a pack is checked against: built from settings that may be stubs, and a verdict, never a raise."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from igni_core.core.auth.schemas import SignedInSession
from igni_core.core.config.group_policy.jws import PackRejected
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.signature import PackSignatureVerifier, VerifiedPack
from igni_core.core.config.group_policy.trust import PackTrust, PackVerdict
from tests.group_policy_signing import ISSUER, KEY, PERSON, sign_in


def _settings(tmp_path, *, api_url=ISSUER, trusted_keys=(), required=False, credentials="creds"):
    return SimpleNamespace(
        api_url=api_url,
        auth=SimpleNamespace(
            credentials_file=str(tmp_path / "credentials.json")
            if credentials == "creds"
            else credentials
        ),
        group_policy=SimpleNamespace(trusted_keys=trusted_keys, required=required),
    )


def _pack() -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1", group_name="G", fetched_at=datetime.now(timezone.utc), entries=[]
    )


def _trust(**kwargs) -> PackTrust:
    kwargs.setdefault("api_url", ISSUER)
    kwargs.setdefault("session", SignedInSession(subject=PERSON))
    return PackTrust(**kwargs)


class TestFromSettings:
    def test_the_session_is_read_from_the_credentials_file(self, tmp_path):
        token = sign_in(tmp_path / "credentials.json", "person-9", org="org-9")

        trust = PackTrust.from_settings(_settings(tmp_path))

        assert trust.session == SignedInSession(subject="person-9", org="org-9", token=token)

    def test_nobody_signed_in_is_an_empty_session(self, tmp_path):
        trust = PackTrust.from_settings(_settings(tmp_path))
        assert trust.session == SignedInSession()

    def test_the_address_pins_and_required_flag_are_carried(self, tmp_path):
        trust = PackTrust.from_settings(_settings(tmp_path, trusted_keys=["a", "b"], required=True))
        assert (trust.api_url, trust.pins, trust.required) == (ISSUER, ("a", "b"), True)

    @pytest.mark.parametrize("api_url", [None, "", 7, ["https://x.invalid"], b"https://x"])
    def test_an_address_that_is_not_text_is_none(self, tmp_path, api_url):
        assert PackTrust.from_settings(_settings(tmp_path, api_url=api_url)).api_url is None

    def test_pins_that_are_not_text_are_dropped(self, tmp_path):
        trust = PackTrust.from_settings(
            _settings(tmp_path, trusted_keys=["keep", 3, None, ["x"], "also"])
        )
        assert trust.pins == ("keep", "also")

    def test_a_tuple_of_pins_is_read_too(self, tmp_path):
        assert PackTrust.from_settings(_settings(tmp_path, trusted_keys=("t",))).pins == ("t",)

    @pytest.mark.parametrize("trusted_keys", [None, "abc", {"a": 1}, 5])
    def test_pins_that_are_not_a_list_are_none_not_split_into_letters(self, tmp_path, trusted_keys):
        assert PackTrust.from_settings(_settings(tmp_path, trusted_keys=trusted_keys)).pins == ()

    @pytest.mark.parametrize("required", ["true", 1, None, "yes"])
    def test_required_is_only_ever_a_real_boolean(self, tmp_path, required):
        assert PackTrust.from_settings(_settings(tmp_path, required=required)).required is False

    def test_a_credentials_file_setting_that_is_not_text_means_the_default_place(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        sign_in(tmp_path / ".igni" / "credentials.json", "from-default")

        trust = PackTrust.from_settings(_settings(tmp_path, credentials=42))

        assert trust.session.subject == "from-default"

    def test_a_corrupt_credentials_file_is_nobody(self, tmp_path):
        (tmp_path / "credentials.json").write_text("{not json")
        assert PackTrust.from_settings(_settings(tmp_path)).session == SignedInSession()


class TestMayFetchUnpinnedKeys:
    @pytest.mark.parametrize(
        "api_url",
        [
            "https://igni.example.invalid",
            "http://localhost:8000",
            "http://127.0.0.1",
            "http://[::1]:9",
        ],
    )
    def test_over_tls_or_from_loopback(self, api_url):
        assert _trust(api_url=api_url).may_fetch_unpinned_keys() is True

    @pytest.mark.parametrize(
        "api_url", ["http://igni.corp.invalid", "ftp://localhost", "igni.example.invalid", None, ""]
    )
    def test_nowhere_else(self, api_url):
        assert _trust(api_url=api_url).may_fetch_unpinned_keys() is False


class TestSignatureVerifier:
    def test_it_holds_the_issuer_the_person_the_org_and_the_pins(self):
        trust = _trust(
            session=SignedInSession(subject="p", org="o"), pins=("t1",), api_url=ISSUER + "/"
        )

        verifier = trust.signature_verifier()

        assert isinstance(verifier, PackSignatureVerifier)
        assert verifier.issuer.normalised == ISSUER
        assert (verifier.subject, verifier.org, verifier.pins) == ("p", "o", ("t1",))


class TestVerify:
    def test_a_genuine_pack_is_ok_and_carries_what_was_verified(self):
        verdict = _trust().verify(KEY.sign(_pack(), version=8), KEY.key_set)

        assert verdict.ok is True
        assert verdict.reason == ""
        assert isinstance(verdict.verified, VerifiedPack)
        assert (verdict.verified.version, verdict.verified.subject) == (8, PERSON)

    def test_a_refused_pack_is_a_verdict_with_the_reason_never_a_raise(self):
        verdict = _trust().verify(KEY.sign(_pack(), sub="other"), KEY.key_set)

        assert verdict.ok is False
        assert verdict.verified is None
        assert "somebody other than the person signed in" in verdict.reason

    @pytest.mark.parametrize("compact", ["", "a.b", "not a jws", "a.b.c.d"])
    def test_something_that_is_not_a_jws_is_a_verdict_too(self, compact):
        verdict = _trust().verify(compact, KEY.key_set)
        assert (verdict.ok, verdict.verified) == (False, None)
        assert "compact JWS" in verdict.reason

    def test_no_keys_is_a_verdict(self):
        verdict = _trust().verify(KEY.sign(_pack()), None)
        assert verdict.ok is False
        assert "no igni server key" in verdict.reason

    def test_an_older_pack_than_the_minimum_is_a_rollback_but_the_minimum_itself_is_not(self):
        compact = KEY.sign(_pack(), version=5)
        assert _trust().verify(compact, KEY.key_set, min_version=5).ok is True
        refused = _trust().verify(compact, KEY.key_set, min_version=6)
        assert refused.ok is False
        assert "rollback" in refused.reason

    def test_no_minimum_accepts_any_version(self):
        assert _trust().verify(KEY.sign(_pack(), version=0), KEY.key_set).ok is True

    def test_pins_narrow_the_keys(self):
        assert _trust(pins=("somebody-elses",)).verify(KEY.sign(_pack()), KEY.key_set).ok is False
        assert _trust(pins=(KEY.thumbprint,)).verify(KEY.sign(_pack()), KEY.key_set).ok is True

    def test_what_is_not_a_rejection_is_not_swallowed(self, monkeypatch):
        def boom(self, *args, **kwargs):
            raise RuntimeError("a bug, not a verdict")

        monkeypatch.setattr(PackSignatureVerifier, "verify", boom)
        with pytest.raises(RuntimeError):
            _trust().verify(KEY.sign(_pack()), KEY.key_set)

    def test_the_reason_is_the_rejections_message(self, monkeypatch):
        def refuse(self, *args, **kwargs):
            raise PackRejected("exactly this")

        monkeypatch.setattr(PackSignatureVerifier, "verify", refuse)
        assert _trust().verify("a.b.c", None) == PackVerdict(ok=False, reason="exactly this")


class TestFrozen:
    def test_a_trust_and_a_verdict_cannot_be_changed(self):
        with pytest.raises(ValueError):
            _trust().required = True  # type: ignore[misc]
        with pytest.raises(ValueError):
            PackVerdict(ok=True).ok = False  # type: ignore[misc]

    def test_the_defaults_trust_nobody_and_require_nothing(self):
        trust = PackTrust()
        assert (trust.api_url, trust.session, trust.pins, trust.required) == (
            None,
            SignedInSession(),
            (),
            False,
        )
