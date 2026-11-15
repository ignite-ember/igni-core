"""``igni -m`` with a shared database scopes it to the person signed in.

Only the backend told storage tenancy who is signed in
(``ProcessEnvironment.enter``), so a headless run with ``IGNI_DATABASE_URL``
crashed with a ``NoTenantError`` traceback whether anyone was signed in or
not (campaign R025). The headless run now says the same, and with nobody
signed in it refuses in one line rather than a traceback.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from igni_core.cli import invocation as invocation_module
from igni_core.cli.headless_run import HeadlessRun
from igni_core.cli.invocation import CliInvocation
from igni_core.cli.options import CliOptions
from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.config.settings import Settings
from igni_core.core.db.scope import NoTenantError, Tenant, tenancy


def _jwt(claims: dict[str, Any]) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


@pytest.fixture(autouse=True)
def _no_tenancy():
    tenancy.use(None)
    yield
    tenancy.use(None)


def _invocation(credentials: Path) -> CliInvocation:
    settings = Settings()
    settings.auth.credentials_file = str(credentials)
    return CliInvocation(CliOptions(), settings, HeadlessRun(drain_timeout_seconds=0.05))


def test_the_run_scopes_storage_to_the_person_signed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = tmp_path / "credentials.json"
    token = _jwt({"sub": "u-1", "org": "org-acme"})
    CredentialsStore(str(credentials)).save(Credentials.new(token, "who@acme.test"))
    seen: list[Tenant | None] = []

    async def session(self, message: str) -> None:
        seen.append(tenancy.tenant())

    monkeypatch.setattr(invocation_module.CliInvocation, "_session", session)
    _invocation(credentials).run_message("hi")

    assert seen == [Tenant(org="org-acme", producer="person:u-1")]


def test_nobody_signed_in_is_one_line_and_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def session(self, message: str) -> None:
        tenancy.scope(shared=True)

    monkeypatch.setattr(invocation_module.CliInvocation, "_session", session)
    with pytest.raises(SystemExit) as exc:
        _invocation(tmp_path / "none.json").run_message("hi")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "nobody is signed in" in err
    assert "igni login" in err


def test_the_error_is_still_the_scope_s_own():
    with pytest.raises(NoTenantError):
        tenancy.scope(shared=True)
