"""``BrowserSignIn.login``: what a person sees when the browser flow ends.

The portal and the callback server are collaborators and are replaced
with small fakes (no port, no network); the credentials store is the real
one on a tmp path, so what is asserted is what lands on disk.
"""

from __future__ import annotations

import asyncio
import webbrowser

import pytest

from igni_core.backend.browser_sign_in import BrowserSignIn, _StatusForwarder
from igni_core.core.auth.credentials import CredentialsStore
from igni_core.core.auth.schemas import UserInfo, ValidateResult
from igni_core.core.config.endpoint import ApiUrlNotConfigured

URL = "https://portal.example/cli-auth?port=4242"


class _Callback:
    port = 4242

    def __init__(self, token):
        self._token = token
        self.waited_for = None

    async def wait_for_token(self, timeout):
        self.waited_for = timeout
        return self._token


class _Portal:
    def __init__(self, token="tok-1", validation=None, start_error=None):
        self.callback = _Callback(token)
        self.validation = validation
        self.start_error = start_error
        self.validated: list[str] = []

    def start_callback(self):
        if self.start_error:
            raise self.start_error
        portal = self

        class _Ctx:
            async def __aenter__(self_inner):
                return portal.callback

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()

    def login_url(self, port):
        assert port == 4242
        return URL

    async def validate_token(self, token):
        self.validated.append(token)
        return self.validation


class _Pack:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []

    async def hydrate(self, token, *, force=False):
        self.calls.append((token, force))
        return False  # a failed refresh must not fail the sign-in


def _ok(email="ada@acme.io"):
    return ValidateResult(ok=True, user=UserInfo(email=email, name="Ada"))


@pytest.fixture
def opened(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: urls.append(url) or True)
    return urls


@pytest.fixture
def store(tmp_path):
    return CredentialsStore(str(tmp_path / "credentials.json"))


async def test_a_sign_in_keeps_the_token_and_email_and_refreshes_the_pack(store, opened):
    portal, pack = _Portal(validation=_ok()), _Pack()

    result = await BrowserSignIn(portal, store, pack).login()

    assert (result.ok, result.email) == (True, "ada@acme.io")
    saved = store.load().creds
    assert (saved.access_token, saved.email) == ("tok-1", "ada@acme.io")
    assert opened == [URL]
    assert portal.callback.waited_for == BrowserSignIn.CALLBACK_TIMEOUT == 300
    assert pack.calls == [("tok-1", True)], "force, or a login inside the 5-minute window is stale"


async def test_a_token_that_fails_validation_still_signs_in_with_no_email(store, opened):
    portal = _Portal(validation=ValidateResult(ok=False, reason="network_error"))

    result = await BrowserSignIn(portal, store, _Pack()).login()

    assert (result.ok, result.email) == (True, "")
    assert store.load().creds.access_token == "tok-1"


async def test_a_login_that_times_out_saves_nothing_and_leaves_the_pack_alone(store, opened):
    portal, pack = _Portal(token=None, validation=_ok()), _Pack()

    result = await BrowserSignIn(portal, store, pack).login()

    assert (result.ok, result.error) == (False, "Login timed out")
    assert not store.load().ok
    assert portal.validated == [] and pack.calls == []


async def test_an_unconfigured_server_is_a_failed_login_with_its_message(store, opened):
    portal = _Portal(start_error=ApiUrlNotConfigured("Signing in needs an igni server"))

    result = await BrowserSignIn(portal, store, _Pack()).login()

    assert result.ok is False
    assert "Signing in needs an igni server" in result.error


async def test_a_credentials_file_that_cannot_be_written_is_a_failed_login(tmp_path, opened):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = CredentialsStore(str(blocker / "credentials.json"))  # parent is a file
    pack = _Pack()

    result = await BrowserSignIn(_Portal(validation=_ok()), store, pack).login()

    assert result.ok is False and result.error
    assert pack.calls == [], "no pack refresh for a sign-in that was not kept"


async def test_an_unexpected_error_is_a_bug_and_raises(store, opened):
    portal = _Portal(start_error=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await BrowserSignIn(portal, store, _Pack()).login()


@pytest.mark.parametrize("error", [webbrowser.Error("no runner"), OSError("no display")])
async def test_a_browser_that_will_not_open_still_prints_the_address(store, monkeypatch, error):
    def _fail(url, *a, **k):
        raise error

    monkeypatch.setattr(webbrowser, "open", _fail)
    lines: list[str] = []

    result = await BrowserSignIn(_Portal(validation=_ok()), store, _Pack()).login(lines.append)

    assert result.ok is True
    waiting = [line for line in lines if line.startswith("Waiting for login")]
    assert waiting == [
        f"Waiting for login in browser...\nIf the browser didn't open, go to:\n{URL}"
    ]


async def test_progress_lines_come_in_order_to_a_sync_callback(store, opened):
    lines: list[str] = []

    await BrowserSignIn(_Portal(validation=_ok()), store, _Pack()).login(lines.append)

    assert [line.split("\n")[0] for line in lines] == [
        "Starting local server...",
        "Waiting for login in browser...",
        "Fetching user info...",
    ]


async def test_an_async_status_callback_is_scheduled_not_awaited(store, opened):
    seen: list[str] = []

    async def on_status(text: str) -> None:
        seen.append(text)

    await BrowserSignIn(_Portal(validation=_ok()), store, _Pack()).login(on_status)
    await asyncio.sleep(0.05)

    assert seen[0] == "Starting local server..." and len(seen) == 3


def test_no_status_callback_is_a_no_op():
    _StatusForwarder(None).emit("anything")  # must not raise


async def test_a_status_callback_that_raises_fails_the_login(store, opened):
    """The forwarder does not shield the flow from its caller's callback."""

    def bad(_text: str) -> None:
        raise ValueError("echo failed")

    with pytest.raises(ValueError, match="echo failed"):
        await BrowserSignIn(_Portal(validation=_ok()), store, _Pack()).login(bad)
