"""``igni login`` signs in from a terminal, and from a script.

Signing in was a slash command inside the app (``/login``); a terminal
had no way to do it, so ``igni ext sync`` and ``igni -m`` could not be
signed in without the app. ``--token-stdin`` is the scripted form.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from igni_core.backend.schemas_rpc import LoginResult
from igni_core.cli import cli
from igni_core.core.auth.credentials import CredentialsStore
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.auth.schemas import UserInfo, ValidateResult

login_cli = importlib.import_module("igni_core.cli.login")


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    value = SimpleNamespace(
        api_url="https://igni.test",
        portal_url="",
        auth=SimpleNamespace(credentials_file=str(tmp_path / "credentials.json")),
        storage=SimpleNamespace(data_dir=str(tmp_path / "data")),
    )
    monkeypatch.setattr(login_cli.SettingsLoader, "load", classmethod(lambda cls: value))
    return value


def _validated(monkeypatch, *, ok: bool = True) -> None:
    async def validate(self, token: str) -> ValidateResult:
        if not ok:
            return ValidateResult(ok=False, reason="http_error", status_code=401)
        return ValidateResult(ok=True, user=UserInfo(email="ada@acme.test"), status_code=200)

    monkeypatch.setattr(PortalClient, "validate_token", validate)


def test_a_token_on_stdin_is_checked_and_kept(settings, monkeypatch):
    _validated(monkeypatch)
    result = CliRunner().invoke(cli, ["login", "--token-stdin"], input="tok-123\n")
    assert result.exit_code == 0, result.output
    assert "signed in to https://igni.test as ada@acme.test" in result.output
    stored = CredentialsStore(settings.auth.credentials_file).load()
    assert stored.creds is not None and stored.creds.access_token == "tok-123"


def test_a_token_igni_refuses_is_not_kept(settings, monkeypatch):
    _validated(monkeypatch, ok=False)
    result = CliRunner().invoke(cli, ["login", "--token-stdin"], input="nope\n")
    assert result.exit_code != 0
    assert "401" in result.output
    assert not Path(settings.auth.credentials_file).exists()


def test_login_with_no_server_names_the_setting(settings):
    settings.api_url = ""
    result = CliRunner().invoke(cli, ["login", "--token-stdin"], input="tok\n")
    assert result.exit_code != 0
    assert "api_url" in result.output


def test_the_browser_flow_prints_where_to_sign_in(settings, monkeypatch):
    async def login(self, on_status=None):
        on_status("Waiting for login in browser...\nIf the browser didn't open, go to:\nhttps://x")
        return LoginResult(ok=True, email="ada@acme.test")

    monkeypatch.setattr(login_cli.BrowserSignIn, "login", login)
    result = CliRunner().invoke(cli, ["login"])
    assert result.exit_code == 0, result.output
    assert "https://x" in result.output
    assert "ada@acme.test" in result.output


# ── what a failed or odd sign-in says ───────────────────────────────


def _rejected(monkeypatch, result: ValidateResult) -> None:
    async def validate(self, token: str) -> ValidateResult:
        return result

    monkeypatch.setattr(PortalClient, "validate_token", validate)


@pytest.mark.parametrize(
    "result,said",
    [
        (ValidateResult(ok=False, reason="http_error", status_code=403), "(HTTP 403)"),
        (
            ValidateResult(ok=False, reason="network_error", error="connection refused"),
            "(connection refused)",
        ),
        (ValidateResult(ok=False, reason="decode_error"), "(decode_error)"),
    ],
)
def test_a_refused_token_says_why_and_saves_nothing(settings, monkeypatch, result, said):
    _rejected(monkeypatch, result)
    out = CliRunner().invoke(cli, ["login", "--token-stdin"], input="tok\n")
    assert out.exit_code != 0
    assert f"igni server did not accept the token {said}" in out.output
    assert not Path(settings.auth.credentials_file).exists()


@pytest.mark.parametrize("stdin", ["", "  \n\t\n"])
def test_no_token_on_stdin_is_refused_before_igni_server_is_asked(settings, monkeypatch, stdin):
    asked = []

    async def validate(self, token: str) -> ValidateResult:
        asked.append(token)
        return ValidateResult(ok=True)

    monkeypatch.setattr(PortalClient, "validate_token", validate)
    out = CliRunner().invoke(cli, ["login", "--token-stdin"], input=stdin)
    assert out.exit_code != 0
    assert "no token on stdin" in out.output
    assert asked == [] and not Path(settings.auth.credentials_file).exists()


def test_the_token_is_stripped_before_it_is_checked_and_kept(settings, monkeypatch):
    seen = []

    async def validate(self, token: str) -> ValidateResult:
        seen.append(token)
        return ValidateResult(ok=True, user=UserInfo(email="ada@acme.test"), status_code=200)

    monkeypatch.setattr(PortalClient, "validate_token", validate)
    out = CliRunner().invoke(cli, ["login", "--token-stdin"], input="  tok-9 \r\n")
    assert out.exit_code == 0, out.output
    assert seen == ["tok-9"]
    stored = CredentialsStore(settings.auth.credentials_file).load()
    assert stored.creds is not None
    assert (stored.creds.access_token, stored.creds.email) == ("tok-9", "ada@acme.test")


def test_an_accepted_token_with_no_account_name_is_kept_and_named_unnamed(settings, monkeypatch):
    _rejected(monkeypatch, ValidateResult(ok=True, status_code=200))
    out = CliRunner().invoke(cli, ["login", "--token-stdin"], input="tok\n")
    assert out.exit_code == 0, out.output
    assert "signed in to https://igni.test as an unnamed account" in out.output
    assert CredentialsStore(settings.auth.credentials_file).load().creds is not None


def test_a_browser_sign_in_that_fails_says_what_went_wrong(settings, monkeypatch):
    async def login(self, on_status=None):
        return LoginResult(ok=False, error="the portal said no")

    monkeypatch.setattr(login_cli.BrowserSignIn, "login", login)
    out = CliRunner().invoke(cli, ["login"])
    assert out.exit_code != 0
    assert "signing in failed: the portal said no" in out.output
    assert "signed in to" not in out.output


def test_a_browser_sign_in_nobody_finishes_gives_up_at_the_timeout(settings, monkeypatch):
    async def login(self, on_status=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(login_cli.BrowserSignIn, "login", login)
    out = CliRunner().invoke(cli, ["login", "--timeout", "0.05"])
    assert out.exit_code != 0
    assert "nobody signed in within 0.05s" in out.output


def test_a_browser_sign_in_with_no_email_is_named_unnamed(settings, monkeypatch):
    async def login(self, on_status=None):
        return LoginResult(ok=True)

    monkeypatch.setattr(login_cli.BrowserSignIn, "login", login)
    out = CliRunner().invoke(cli, ["login"])
    assert out.exit_code == 0, out.output
    assert "signed in to https://igni.test as an unnamed account" in out.output


def test_the_browser_flow_hydrates_the_group_pack_with_the_same_settings_and_portal(
    settings, monkeypatch
):
    wired = {}
    original = login_cli.BrowserSignIn.__init__

    def init(self, portal, store, hydrator):
        wired.update(portal=portal, store=store, hydrator=hydrator)
        original(self, portal, store, hydrator)

    async def login(self, on_status=None):
        return LoginResult(ok=True, email="ada@acme.test")

    monkeypatch.setattr(login_cli.BrowserSignIn, "__init__", init)
    monkeypatch.setattr(login_cli.BrowserSignIn, "login", login)
    settings.portal_url = "https://portal.test"
    out = CliRunner().invoke(cli, ["login"])
    assert out.exit_code == 0, out.output
    assert isinstance(wired["hydrator"], login_cli.GroupPackHydrator)
    assert wired["portal"].api_url == "https://igni.test"
    assert wired["portal"].portal_url == "https://portal.test"
    assert isinstance(wired["store"], CredentialsStore)


def test_an_empty_portal_setting_means_the_portal_is_derived_from_the_server(settings, monkeypatch):
    seen = []

    async def validate(self, token: str) -> ValidateResult:
        seen.append(self)
        return ValidateResult(ok=True, status_code=200)

    monkeypatch.setattr(PortalClient, "validate_token", validate)
    settings.portal_url = ""
    out = CliRunner().invoke(cli, ["login", "--token-stdin"], input="tok\n")
    assert out.exit_code == 0, out.output
    assert seen[0].portal_url == PortalClient(api_url="https://igni.test").portal_url
