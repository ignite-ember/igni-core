"""The saved sign-in: what is written, what is read back, and who it says is signed in.

``test_auth.py`` pins the basics (default path, save and load, expiry of
the file). This file pins the rest of what ``credentials.json`` and the
layer's credential source do: the refusals ``load`` names, the file's
mode, the lifetime a token's own ``exp`` gives its record, the identity
a session carries, and where a layer's source replaces the file.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from igni_core.core.auth.credentials import (
    DEFAULT_TOKEN_TTL,
    CloudCredentials,
    Credentials,
    CredentialSource,
    CredentialsStore,
    EmptyCloudCredentials,
)
from igni_core.core.auth.schemas import LoadCredentialsResult
from igni_core.core.layer import Layer, layers


def _jwt(claims: dict | str) -> str:
    def part(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    body = claims if isinstance(claims, str) else json.dumps(claims)
    return f"{part(b'{}')}.{part(body.encode())}.sig"


def _write(path: Path, token: str, **fields: object) -> None:
    record = {"access_token": token, "email": "ada@acme.test", **fields}
    path.write_text(json.dumps(record))


def _seconds_until(stamp: str) -> float:
    return (datetime.fromisoformat(stamp) - datetime.now(timezone.utc)).total_seconds()


class TestWhatLoadSays:
    def test_a_directory_where_the_file_should_be_is_an_error(self, tmp_path):
        result = CredentialsStore(str(tmp_path)).load()
        assert (result.ok, result.reason) == (False, "error")
        assert result.error

    def test_json_that_is_not_an_object_does_not_fit(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("[1, 2]")
        result = CredentialsStore(str(path)).load()
        assert (result.ok, result.reason) == (False, "schema_mismatch")

    def test_a_record_without_a_token_does_not_fit(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"email": "ada@acme.test"}))
        result = CredentialsStore(str(path)).load()
        assert (result.ok, result.reason) == (False, "schema_mismatch")

    def test_an_empty_path_means_the_default_file(self):
        assert CredentialsStore("").path == CredentialsStore().path


class TestWhatSaveWrites:
    def test_the_file_is_private_and_its_folder_is_made(self, tmp_path):
        path = tmp_path / "deeper" / "still" / "c.json"
        CredentialsStore(str(path)).save(Credentials.new("t", "a@b.c"))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_an_existing_loose_file_becomes_private_when_overwritten(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{}")
        os.chmod(path, 0o644)
        CredentialsStore(str(path)).save(Credentials.new("t", "a@b.c"))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert CredentialsStore(str(path)).load().ok

    def test_a_new_record_is_a_bearer_token_valid_for_thirty_days(self):
        creds = Credentials.new("t", "a@b.c")
        assert creds.token_type == "bearer"
        assert DEFAULT_TOKEN_TTL == 30 * 24 * 3600
        assert _seconds_until(creds.expires_at) == pytest.approx(DEFAULT_TOKEN_TTL, abs=5)
        assert datetime.fromisoformat(creds.created_at).tzinfo is not None


class TestALifetimeFromTheToken:
    def test_the_record_expires_when_the_token_does(self):
        token = _jwt({"exp": int(time.time()) + 900})
        creds = Credentials.for_token(token, "a@b.c")
        assert _seconds_until(creds.expires_at) == pytest.approx(900, abs=5)
        assert creds.access_token == token
        assert creds.email == "a@b.c"

    def test_a_token_already_past_its_exp_is_expired_not_negative(self):
        creds = Credentials.for_token(_jwt({"exp": int(time.time()) - 3600}), "a@b.c")
        assert creds.is_expired()
        assert _seconds_until(creds.expires_at) <= 0.5
        assert _seconds_until(creds.expires_at) > -5

    @pytest.mark.parametrize(
        "token",
        [_jwt({"sub": "u"}), "not-a-jwt", _jwt({"exp": "soon"}), _jwt("[1]")],
        ids=["no-exp", "not-a-jwt", "exp-not-a-number", "payload-not-an-object"],
    )
    def test_a_token_with_no_usable_exp_gets_the_thirty_day_default(self, token):
        creds = Credentials.for_token(token, "a@b.c")
        assert _seconds_until(creds.expires_at) == pytest.approx(DEFAULT_TOKEN_TTL, abs=5)


class TestWhenTheRecordExpires:
    def test_an_expiry_without_a_zone_is_read_as_utc(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(tzinfo=None)
        assert Credentials(access_token="t", expires_at=past.isoformat()).is_expired()
        assert not Credentials(access_token="t", expires_at=future.isoformat()).is_expired()

    def test_an_expiry_nobody_can_read_is_not_an_expiry(self):
        assert not Credentials(access_token="t", expires_at="next tuesday").is_expired()


class TestWhoIsSignedIn:
    def test_nobody_when_there_is_no_file(self, tmp_path):
        session = CredentialsStore(str(tmp_path / "none.json")).signed_in_session()
        assert (session.subject, session.org, session.token) == (None, None, None)

    def test_nobody_when_the_file_is_unreadable(self, tmp_path):
        session = CredentialsStore(str(tmp_path)).signed_in_session()
        assert (session.subject, session.org, session.token) == (None, None, None)

    def test_the_subject_org_and_token_of_the_stored_session(self, tmp_path):
        path = tmp_path / "c.json"
        token = _jwt({"sub": "u-ada", "org": "org-acme"})
        _write(path, token)
        session = CredentialsStore(str(path)).signed_in_session()
        assert (session.subject, session.org, session.token) == ("u-ada", "org-acme", token)

    @pytest.mark.parametrize(
        "token", ["opaque", _jwt({"sub": 5})], ids=["undecodable", "claims-do-not-fit"]
    )
    def test_a_token_with_no_readable_claims_is_still_the_token(self, tmp_path, token):
        path = tmp_path / "c.json"
        _write(path, token)
        session = CredentialsStore(str(path)).signed_in_session()
        assert (session.subject, session.org, session.token) == (None, None, token)


class TestTheViewOfTheSignIn:
    def test_the_identity_the_token_names(self, tmp_path):
        path = tmp_path / "c.json"
        token = _jwt(
            {"sub": "u-ada", "kind": "person", "group": "eng", "org": "o", "org_name": "Acme"}
        )
        _write(
            path, token, expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        )
        creds = CloudCredentials(str(path))
        assert creds.is_authenticated
        assert creds.email == "ada@acme.test"
        assert (creds.user_id, creds.kind, creds.group) == ("u-ada", "person", "eng")
        assert (creds.org_id, creds.org_name) == ("o", "Acme")
        assert creds.claims is not None and creds.claims.sub == "u-ada"

    def test_nobody_is_signed_in_without_a_file(self, tmp_path):
        creds = CloudCredentials(str(tmp_path / "none.json"))
        assert not creds.is_authenticated
        assert creds.access_token is None
        assert creds.email is None
        assert (creds.user_id, creds.kind, creds.group, creds.claims) == (None, None, None, None)

    def test_an_expired_record_is_nobody(self, tmp_path):
        path = tmp_path / "c.json"
        _write(
            path,
            _jwt({"sub": "u"}),
            expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )
        creds = CloudCredentials(str(path))
        assert not creds.is_authenticated
        assert creds.email is None
        assert creds.user_id is None

    def test_a_token_whose_claims_do_not_fit_is_still_signed_in(self, tmp_path):
        path = tmp_path / "c.json"
        _write(path, _jwt({"exp": "soon", "sub": 5}))
        creds = CloudCredentials(str(path))
        assert creds.is_authenticated
        assert creds.access_token == _jwt({"exp": "soon", "sub": 5})
        assert (creds.user_id, creds.org_id, creds.claims) == (None, None, None)

    def test_the_file_is_read_once_per_instance(self, tmp_path):
        path = tmp_path / "c.json"
        _write(path, "first")
        creds = CloudCredentials(str(path))
        assert creds.access_token == "first"
        _write(path, "second")
        assert creds.access_token == "first"
        assert CloudCredentials(str(path)).access_token == "second"

    def test_a_path_and_a_store_together_are_refused(self, tmp_path):
        with pytest.raises(ValueError, match="either path or store"):
            CloudCredentials(str(tmp_path / "c.json"), store=CredentialsStore())

    def test_a_given_store_is_the_one_read(self, tmp_path):
        path = tmp_path / "c.json"
        _write(path, "from-store")
        assert CloudCredentials(store=CredentialsStore(str(path))).access_token == "from-store"

    def test_the_empty_one_never_reads_a_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            CredentialsStore, "load", lambda self: pytest.fail("the logged-out view read a file")
        )
        creds = CloudCredentials.empty()
        assert isinstance(creds, EmptyCloudCredentials)
        assert isinstance(creds, CloudCredentials)
        assert not creds.is_authenticated
        assert (creds.access_token, creds.email, creds.org_id, creds.org_name) == (None,) * 4
        assert (creds.user_id, creds.kind, creds.group, creds.claims) == (None,) * 4


class _Source(CredentialSource):
    """A layer's own place for the credentials, in memory."""

    def __init__(self) -> None:
        self.kept: Credentials | None = Credentials.new("layer-token", "vm@acme.test")
        self.saved: list[Credentials] = []
        self.cleared = 0
        self.readied = 0

    def load(self) -> LoadCredentialsResult:
        if self.kept is None:
            return LoadCredentialsResult(ok=False, reason="no_file")
        return LoadCredentialsResult(ok=True, creds=self.kept)

    def save(self, creds: Credentials) -> None:
        self.saved.append(creds)

    def clear(self) -> None:
        self.cleared += 1

    async def ready(self) -> None:
        self.readied += 1


class _Bare(CredentialSource):
    def load(self) -> LoadCredentialsResult:
        return LoadCredentialsResult(ok=False, reason="no_file")


def _install(source: CredentialSource) -> None:
    class _Layer(Layer):
        name = "test"

        def credentials(self) -> CredentialSource:
            return source

    layers.install(_Layer())


class TestWhenTheLayerKeepsThem:
    def test_every_operation_goes_to_the_source_and_never_to_the_file(self, tmp_path):
        source = _Source()
        _install(source)
        path = tmp_path / "c.json"
        path.write_text("{}")
        store = CredentialsStore(str(path))

        creds = Credentials.new("saved", "a@b.c")
        store.save(creds)
        assert source.saved == [creds]
        assert store.load().creds is source.kept
        store.clear()
        assert source.cleared == 1
        assert path.read_text() == "{}"

    def test_the_sign_in_the_view_and_the_session_come_from_the_source(self, tmp_path):
        source = _Source()
        source.kept = Credentials.new(_jwt({"sub": "vm-1", "org": "o"}), "vm@acme.test")
        _install(source)
        store = CredentialsStore(str(tmp_path / "unused.json"))
        assert CloudCredentials(store=store).email == "vm@acme.test"
        session = store.signed_in_session()
        assert (session.subject, session.org) == ("vm-1", "o")

    def test_a_source_that_cannot_be_read_is_nobody_signed_in(self, tmp_path):
        source = _Source()

        def unreadable() -> LoadCredentialsResult:
            raise PermissionError("no access")

        source.load = unreadable  # type: ignore[method-assign]
        _install(source)
        session = CredentialsStore(str(tmp_path / "c.json")).signed_in_session()
        assert (session.subject, session.org, session.token) == (None, None, None)

    async def test_ready_waits_for_the_source(self, tmp_path):
        source = _Source()
        _install(source)
        await CredentialsStore(str(tmp_path / "c.json")).ready()
        assert source.readied == 1

    async def test_a_file_is_always_ready(self, tmp_path):
        await CredentialsStore(str(tmp_path / "c.json")).ready()

    async def test_a_source_that_says_nothing_is_ready_clears_nothing_and_refuses_to_save(
        self, tmp_path, caplog
    ):
        _install(_Bare())
        store = CredentialsStore(str(tmp_path / "c.json"))
        with caplog.at_level("WARNING"):
            store.save(Credentials.new("t", "a@b.c"))
        store.clear()
        await store.ready()
        assert "credentials come from _Bare; not saving them" in caplog.text
        assert not (tmp_path / "c.json").exists()
