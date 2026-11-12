"""``/login``, ``/logout``, ``/whoami`` and the model switch a sign-out makes.

The credentials store is the real one on a tmp path, the models are a real
``ModelsConfig``; only the session around them is a namespace, with the two
calls a sign-out makes on it recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from igni_core.backend.builtin_command_registry import BUILTIN_REGISTRY
from igni_core.backend.cloud_sign_out import switch_off_cloud
from igni_core.backend.cmd_auth import AuthCommand
from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.config.schemas.models import CLOUD_TOKEN_SENTINEL, ModelsConfig
from igni_core.protocol.messages import CommandAction, CommandResultKind

NO_FALLBACK = (
    "Warning: no models with API keys configured. Add a model with an api_key or /login again."
)


def _models(default: str, **registry: str) -> ModelsConfig:
    return ModelsConfig(
        default=default,
        registry={name: {"model_id": name, "api_key": key} for name, key in registry.items()},
    )


def _session(models: ModelsConfig) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(models=models),
        cloud_auth=SimpleNamespace(clear=MagicMock()),
        rebuild_main_team=MagicMock(),
    )


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


@pytest.fixture
def store(tmp_path):
    return CredentialsStore(str(tmp_path / "credentials.json"))


# ── switch_off_cloud ──────────────────────────────────────────────


def test_a_model_with_its_own_key_is_left_alone_and_nothing_is_rebuilt():
    models = _models("own", own="sk-own", cloud=CLOUD_TOKEN_SENTINEL)
    session = _session(models)

    outcome = switch_off_cloud(session)

    assert (outcome.switched, outcome.fallback_model, outcome.warning) == (False, None, None)
    assert models.default == "own"
    session.rebuild_main_team.assert_not_called()


def test_a_session_with_no_models_needs_no_switch():
    session = _session(_models(""))

    assert switch_off_cloud(session).switched is False
    session.rebuild_main_team.assert_not_called()


def test_a_cloud_model_gives_way_to_the_first_one_with_its_own_key_and_the_team_is_rebuilt():
    models = _models("cloud", cloud=CLOUD_TOKEN_SENTINEL, empty="", first="sk-1", second="sk-2")
    session = _session(models)

    outcome = switch_off_cloud(session)

    assert (outcome.switched, outcome.fallback_model) == (True, "first")
    assert models.default == "first"
    session.rebuild_main_team.assert_called_once_with()


def test_a_cloud_model_with_nothing_to_fall_back_to_warns_and_changes_nothing():
    models = _models("cloud", cloud=CLOUD_TOKEN_SENTINEL)
    session = _session(models)

    outcome = switch_off_cloud(session)

    assert (outcome.switched, outcome.fallback_model) == (False, None)
    assert outcome.warning == NO_FALLBACK
    assert models.default == "cloud"
    session.rebuild_main_team.assert_not_called()


# ── /login ────────────────────────────────────────────────────────


async def test_login_hands_the_browser_flow_to_the_transport(store):
    result = await AuthCommand(_session(_models("")), store).login()

    assert result.kind == CommandResultKind.ACTION
    assert result.action == CommandAction.LOGIN


# ── /whoami ───────────────────────────────────────────────────────


async def test_whoami_without_credentials_says_not_logged_in(store):
    result = await AuthCommand(_session(_models("")), store).whoami()

    assert result.kind == CommandResultKind.INFO
    assert result.content == "Not logged in. Use /login to authenticate."


async def test_whoami_with_a_corrupt_credentials_file_says_not_logged_in(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{not json")

    result = await AuthCommand(_session(_models("")), CredentialsStore(str(path))).whoami()

    assert result.content == "Not logged in. Use /login to authenticate."


async def test_whoami_names_the_user_and_when_the_session_ends(store):
    expires = _iso(timedelta(days=1))
    store.save(Credentials(access_token="t", email="ada@acme.io", expires_at=expires))

    result = await AuthCommand(_session(_models("")), store).whoami()

    assert result.content == f"Logged in as ada@acme.io (expires: {expires[:19]})"


async def test_whoami_with_no_expiry_says_unknown(store):
    store.save(Credentials(access_token="t", email="ada@acme.io"))

    result = await AuthCommand(_session(_models("")), store).whoami()

    assert result.content == "Logged in as ada@acme.io (expires: unknown)"


async def test_whoami_with_an_expired_session_says_to_log_in_again(store):
    store.save(
        Credentials(access_token="t", email="ada@acme.io", expires_at=_iso(-timedelta(hours=1)))
    )

    result = await AuthCommand(_session(_models("")), store).whoami()

    assert result.content == "Session expired for ada@acme.io. Use /login to re-authenticate."


# ── /logout ───────────────────────────────────────────────────────


async def test_logout_forgets_the_file_and_the_session_and_says_who(store):
    store.save(Credentials(access_token="t", email="ada@acme.io"))
    session = _session(_models("own", own="sk-own"))

    result = await AuthCommand(session, store).logout()

    assert result.kind == CommandResultKind.INFO
    assert result.action == CommandAction.LOGOUT
    assert result.content == "Logged out (ada@acme.io)."
    assert not store.load().ok
    session.cloud_auth.clear.assert_called_once_with()


async def test_logout_when_not_logged_in_says_so_and_still_clears_the_session(store):
    session = _session(_models(""))

    result = await AuthCommand(session, store).logout()

    assert result.content == "Not logged in."
    session.cloud_auth.clear.assert_called_once_with()


async def test_logout_reports_the_model_it_switched_to(store):
    store.save(Credentials(access_token="t", email="ada@acme.io"))
    models = _models("cloud", cloud=CLOUD_TOKEN_SENTINEL, own="sk-own")

    result = await AuthCommand(_session(models), store).logout()

    assert result.content == (
        "Logged out (ada@acme.io).\nSwitched to own (cloud model no longer available)."
    )
    assert models.default == "own"


async def test_logout_warns_when_a_cloud_model_has_no_fallback(store):
    store.save(Credentials(access_token="t", email="ada@acme.io"))
    models = _models("cloud", cloud=CLOUD_TOKEN_SENTINEL)

    result = await AuthCommand(_session(models), store).logout()

    assert result.content == f"Logged out (ada@acme.io).\n{NO_FALLBACK}"


# ── through the registry ──────────────────────────────────────────


async def test_whoami_typed_as_a_slash_command_uses_the_default_store(tmp_path, monkeypatch):
    """No store is injected: the shim builds the default one, and a shim
    that takes no arguments is not handed the words after the command."""
    monkeypatch.setenv("HOME", str(tmp_path))
    handler = SimpleNamespace(session=_session(_models("")))

    result = await BUILTIN_REGISTRY.dispatch(handler, "/whoami", "ignored words")

    assert result.content == "Not logged in. Use /login to authenticate."
