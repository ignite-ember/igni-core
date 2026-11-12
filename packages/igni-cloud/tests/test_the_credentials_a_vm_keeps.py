"""The credentials a producer's VM keeps: a token in a file, never a store.

``docs/CLOUD.md`` §9. The token the gateway hands the VM lives in the
file ``IGNI_ACCESS_TOKEN_FILE`` names and is rewritten when it is
renewed. The file wins over the environment variable, an expired token
is never handed out, and two sources keep their own keeper.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from igni_cloud.credentials import EnvironmentCredentials


class Keeper:
    """A token keeper, as the VM's own credential is one."""

    def __init__(self, expired: bool = False) -> None:
        self._expired = expired
        self.renewals = 0

    def expired(self) -> bool:
        return self._expired

    async def ready(self) -> None:
        self.renewals += 1


@pytest.fixture(autouse=True)
def no_environment_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment starts empty: only what a test puts there is there."""
    for name in (
        EnvironmentCredentials.TOKEN_FILE_ENV,
        EnvironmentCredentials.TOKEN_ENV,
        EnvironmentCredentials.EMAIL_ENV,
        EnvironmentCredentials.EXPIRES_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def test_the_variables_are_the_ones_the_gateway_sets():
    assert EnvironmentCredentials.TOKEN_FILE_ENV == "IGNI_ACCESS_TOKEN_FILE"
    assert EnvironmentCredentials.TOKEN_ENV == "IGNI_ACCESS_TOKEN"
    assert EnvironmentCredentials.EMAIL_ENV == "IGNI_EMAIL"
    assert EnvironmentCredentials.EXPIRES_ENV == "IGNI_ACCESS_TOKEN_EXPIRES_AT"


def test_the_token_file_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text("from-the-file\n")
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(token_file))
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, "from-the-environment")
    assert EnvironmentCredentials().token() == "from-the-file"


def test_a_named_file_that_is_not_there_gives_no_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The file was named, so the environment variable is not used instead."""
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(tmp_path / "not-there"))
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, "from-the-environment")
    source = EnvironmentCredentials()
    assert source.token() == ""
    assert not source.load().ok


def test_a_file_that_cannot_be_read_gives_no_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(tmp_path))  # a directory
    source = EnvironmentCredentials()
    assert source.token() == ""
    assert not source.load().ok


def test_a_load_without_a_token_says_why():
    result = EnvironmentCredentials().load()
    assert not result.ok
    assert result.reason == "no_file"


def test_a_load_carries_the_email_and_the_expiry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, "the-token")
    monkeypatch.setenv(EnvironmentCredentials.EMAIL_ENV, "ada@example.com")
    monkeypatch.setenv(EnvironmentCredentials.EXPIRES_ENV, "2026-11-12T10:00:00Z")
    result = EnvironmentCredentials().load()
    assert result.ok
    assert result.creds is not None
    assert result.creds.access_token == "the-token"
    assert result.creds.email == "ada@example.com"
    assert result.creds.expires_at == "2026-11-12T10:00:00Z"


def test_an_expired_token_is_never_handed_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text("the-token")
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(token_file))
    source = EnvironmentCredentials()
    source.kept_by(Keeper(expired=True))
    assert source.token() == ""
    assert not source.load().ok


def test_two_sources_keep_their_own_keeper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """One VM's keeper does not gate another's token."""
    token_file = tmp_path / "token"
    token_file.write_text("the-token")
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(token_file))
    expired, fresh = EnvironmentCredentials(), EnvironmentCredentials()
    expired.kept_by(Keeper(expired=True))
    fresh.kept_by(Keeper())
    assert expired.token() == ""
    assert fresh.token() == "the-token"


def test_a_keeper_that_is_taken_away_is_no_longer_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    token_file = tmp_path / "token"
    token_file.write_text("the-token")
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(token_file))
    source = EnvironmentCredentials()
    source.kept_by(Keeper(expired=True))
    assert source.token() == ""
    source.kept_by(None)
    assert source.token() == "the-token"


async def test_ready_waits_for_the_keepers_renewal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, "the-token")
    keeper = Keeper()
    source = EnvironmentCredentials()
    source.kept_by(keeper)
    await source.ready()
    assert keeper.renewals == 1


async def test_ready_without_a_keeper_returns_at_once(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, "the-token")
    await EnvironmentCredentials().ready()


def test_the_token_is_read_again_on_every_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The gateway rewrites the file when the token is renewed."""
    token_file = tmp_path / "token"
    token_file.write_text("first")
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(token_file))
    source = EnvironmentCredentials()
    assert source.token() == "first"
    fresh = tmp_path / "token.new"
    fresh.write_text("second")
    fresh.replace(token_file)
    assert source.token() == "second"
