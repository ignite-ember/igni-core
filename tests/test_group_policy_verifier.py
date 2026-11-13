"""What the async pack verifier refuses before and after fetching keys, and what it accepts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from igni_core.core.auth.schemas import SignedInSession
from igni_core.core.config.group_policy.answers import SignedAbsence
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.trust import PackTrust
from igni_core.core.config.group_policy.verifier import AcceptedPack, PackVerifier, Verification
from tests.group_policy_signing import ISSUER, KEY, PERSON, ServerKey

STRANGER = ServerKey()


def _pack(name: str = "Regulated") -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name=name,
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(kind="settings", entry_name="p", content="{}", content_type="json")
        ],
    )


class Keys:
    """A stand-in for the live JWKS fetch that counts its calls."""

    def __init__(self, result=KEY.key_set, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _verify(answer, keys: Keys, *, api_url: str = ISSUER, pins: tuple[str, ...] = ()):
    trust = PackTrust(api_url=api_url, session=SignedInSession(subject=PERSON), pins=pins)
    return asyncio.run(PackVerifier(trust, keys)(answer))


class TestRefusedBeforeKeysAreFetched:
    @pytest.mark.parametrize("signed", [None, ""])
    def test_an_unsigned_answer_is_refused_without_fetching_keys(self, signed):
        keys = Keys()

        verification = _verify(_pack().model_copy(update={"signed_pack": signed}), keys)

        assert verification == Verification(reason="igni server sent the pack unsigned")
        assert keys.calls == 0

    def test_plain_http_with_no_pin_is_refused_and_says_to_pin(self):
        keys = Keys()
        iss = "http://igni.corp.invalid"

        verification = _verify(KEY.signed(_pack(), iss=iss), keys, api_url=iss)

        assert verification.accepted is None
        assert verification.reason == (
            "'http://igni.corp.invalid' is not HTTPS, so igni server's keys cannot be "
            "fetched safely; pin one in group_policy.trusted_keys"
        )
        assert keys.calls == 0

    def test_plain_http_with_a_pin_fetches_the_keys(self):
        keys = Keys()
        iss = "http://igni.corp.invalid"

        verification = _verify(
            KEY.signed(_pack(), iss=iss), keys, api_url=iss, pins=(KEY.thumbprint,)
        )

        assert verification.accepted is not None
        assert keys.calls == 1

    def test_plain_http_to_loopback_needs_no_pin(self):
        keys = Keys()
        iss = "http://127.0.0.1:8000"
        assert _verify(KEY.signed(_pack(), iss=iss), keys, api_url=iss).accepted is not None
        assert keys.calls == 1


class TestRefusedAfterKeysAreFetched:
    def test_keys_that_could_not_be_fetched_refuse_the_pack(self):
        verification = _verify(KEY.signed(_pack()), Keys(result=None))
        assert verification == Verification(
            reason="igni server's signing keys could not be fetched"
        )

    def test_a_failing_key_fetch_is_not_swallowed(self):
        with pytest.raises(RuntimeError, match="down"):
            _verify(KEY.signed(_pack()), Keys(error=RuntimeError("down")))

    def test_a_pack_signed_by_a_stranger_is_refused_with_the_reason(self):
        verification = _verify(STRANGER.signed(_pack()), Keys())
        assert verification.accepted is None
        assert "does not verify" in verification.reason

    def test_a_pack_for_somebody_else_is_refused(self):
        verification = _verify(KEY.signed(_pack(), sub="other"), Keys())
        assert verification.accepted is None
        assert "somebody other" in verification.reason

    def test_a_signed_pack_presented_as_an_absence_is_refused(self):
        answer = SignedAbsence(signed_pack=KEY.sign(_pack()))
        verification = _verify(answer, Keys())
        assert verification == Verification(
            reason="the signed answer does not say what the response says"
        )

    def test_a_signed_absence_presented_as_a_pack_is_refused(self):
        answer = _pack().model_copy(update={"signed_pack": KEY.sign_absence()})
        verification = _verify(answer, Keys())
        assert verification == Verification(
            reason="the signed answer does not say what the response says"
        )


class TestAccepted:
    def test_a_genuine_pack_carries_the_pack_the_text_the_keys_and_the_version(self):
        signed = KEY.signed(_pack(), version=17)
        keys = Keys()

        verification = _verify(signed, keys)

        accepted = verification.accepted
        assert isinstance(accepted, AcceptedPack)
        assert verification.reason == ""
        assert accepted.compact == signed.signed_pack
        assert accepted.jwks == KEY.key_set
        assert accepted.version == 17
        assert accepted.pack.group_name == "Regulated"
        assert accepted.pack.signed_pack == signed.signed_pack

    def test_the_pack_is_built_from_the_signed_policy_not_the_plain_fields(self):
        compact = KEY.sign(_pack("Signed name"))
        tampered = _pack("Plain name").model_copy(update={"signed_pack": compact})

        assert _verify(tampered, Keys()).accepted.pack.group_name == "Signed name"

    def test_the_etag_the_answer_came_with_is_kept(self):
        signed = KEY.signed(_pack()).model_copy(update={"etag": '"tag-9"'})
        assert _verify(signed, Keys()).accepted.pack.etag == '"tag-9"'

    def test_no_etag_is_none(self):
        assert _verify(KEY.signed(_pack()), Keys()).accepted.pack.etag is None

    def test_a_signed_absence_is_accepted_with_no_pack(self):
        compact = KEY.sign_absence(version=4)
        answer = SignedAbsence(signed_pack=compact, etag='"t"')

        accepted = _verify(answer, Keys()).accepted

        assert accepted.pack is None
        assert accepted.compact == compact
        assert accepted.version == 4
        assert accepted.jwks == KEY.key_set

    @pytest.mark.parametrize("version", [0, 2**62])
    def test_the_extremes_of_a_version_are_carried_unchanged(self, version):
        assert _verify(KEY.signed(_pack(), version=version), Keys()).accepted.version == version

    def test_the_results_cannot_be_changed(self):
        accepted = _verify(KEY.signed(_pack()), Keys()).accepted
        with pytest.raises(ValueError):
            accepted.version = 0  # type: ignore[misc]
        with pytest.raises(ValueError):
            Verification().reason = "x"  # type: ignore[misc]
