"""``SessionCloudAuth``: what the session knows about its account, and the swap invariant.

A real ``SessionCloudAuth`` over real credentials files. The invariant
is the order: the credentials are swapped first, THEN the main team is
rebuilt, so the rebuilt team's prompts and tool bindings carry the new
token — and the rebuild is what a logged-out state needs too.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from igni_core.backend.server_auth import AuthController
from igni_core.core.auth.credentials import CloudCredentials
from igni_core.core.config.user_config_store import UserConfigStore
from igni_core.core.session.cloud_auth import SessionCloudAuth


def _jwt(payload: dict) -> str:
    def part(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return ".".join([part(b'{"alg":"HS256"}'), part(json.dumps(payload).encode()), part(b"sig")])


def _creds(tmp_path: Path, token: str, name: str = "credentials.json") -> CloudCredentials:
    path = tmp_path / name
    path.write_text(json.dumps({"access_token": token, "email": "maya@acme.test"}))
    return CloudCredentials(str(path))


def _auth(creds, rebuild=lambda: None, catalog=None, server_url="https://one.example"):
    return SessionCloudAuth(
        creds=creds, server_url=server_url, rebuild_team=rebuild, catalog=catalog
    )


def test_a_signed_in_session_reports_its_account(tmp_path: Path) -> None:
    token = _jwt({"org": "org_42", "org_name": "Acme"})
    creds = _creds(tmp_path, token)

    auth = _auth(creds)

    assert auth.connected is True
    assert auth.org_id == "org_42"
    assert auth.org_name == "Acme"
    assert auth.access_token == token
    assert auth.credentials is creds


def test_a_logged_out_session_reports_nothing() -> None:
    auth = _auth(CloudCredentials.empty())

    assert auth.connected is False
    assert auth.org_id is None
    assert auth.org_name is None
    assert auth.access_token is None
    assert auth.email is None


def test_replace_swaps_the_credentials_before_the_team_is_rebuilt(tmp_path: Path) -> None:
    fresh = _creds(tmp_path, "fresh-token", "fresh.json")
    seen: list[str | None] = []
    holder: dict[str, SessionCloudAuth] = {}

    def rebuild() -> None:
        # What the rebuilt team would bind: the token the session holds NOW.
        seen.append(holder["auth"].access_token)

    auth = holder["auth"] = _auth(_creds(tmp_path, "old-token", "old.json"), rebuild)

    auth.replace(fresh)

    assert seen == ["fresh-token"]
    assert auth.credentials is fresh
    assert auth.access_token == "fresh-token"


def test_clear_logs_the_session_out_and_rebuilds_the_team_logged_out(tmp_path: Path) -> None:
    seen: list[tuple[bool, str | None]] = []
    holder: dict[str, SessionCloudAuth] = {}

    def rebuild() -> None:
        seen.append((holder["auth"].connected, holder["auth"].access_token))

    auth = holder["auth"] = _auth(_creds(tmp_path, "tok"), rebuild)
    assert auth.connected is True

    auth.clear()
    auth.clear()  # twice is the same state, and each time the team is rebuilt

    assert seen == [(False, None), (False, None)]
    assert auth.connected is False


def test_use_server_changes_the_url_cloud_tools_are_built_with() -> None:
    auth = _auth(CloudCredentials.empty())
    assert auth.server_url == "https://one.example"

    auth.use_server("https://two.example")

    assert auth.server_url == "https://two.example"


def test_choosing_a_server_moves_the_real_session_auth_to_it(tmp_path: Path) -> None:
    """The caller of ``use_server``, with the real object behind it, not a mock."""
    auth = _auth(CloudCredentials.empty(), server_url="https://old.example.com")
    settings = SimpleNamespace(
        api_url="https://old.example.com",
        portal_url="https://portal.old.example.com",
        auth=SimpleNamespace(credentials_file=str(tmp_path / "creds.json")),
    )
    controller = AuthController(
        SimpleNamespace(cloud_auth=auth),  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        status_provider=MagicMock(),
        portal=MagicMock(),
        group_policy=MagicMock(),
        user_config_store=UserConfigStore(tmp_path / "config.yaml"),
        server_discovery=MagicMock(),
    )

    assert not controller.set_server("https://new.example.com").error

    assert auth.server_url == "https://new.example.com"


def test_refresh_models_returns_what_the_catalog_added() -> None:
    catalog = SimpleNamespace(refresh=lambda: 3)

    assert _auth(CloudCredentials.empty(), catalog=catalog).refresh_models() == 3
