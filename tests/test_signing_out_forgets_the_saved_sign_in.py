"""Signing out through ``clear_cloud_credentials`` signs igni out.

It forgot the session's credentials in memory and left the saved ones
on disk, so every other session, and the next launch, was still signed
in — and a cloud model stayed the default with no token behind it.
``/logout`` did it right; the call now does what ``/logout`` does.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from igni_core.backend.server_auth import AuthController
from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.config.schemas.models import CLOUD_TOKEN_SENTINEL, ModelsConfig


def _controller(tmp_path, models: ModelsConfig) -> tuple[AuthController, SimpleNamespace, str]:
    path = str(tmp_path / "credentials.json")
    CredentialsStore(path).save(Credentials(access_token="tok", email="ada@acme.io"))
    session = SimpleNamespace(
        settings=SimpleNamespace(models=models),
        cloud_auth=SimpleNamespace(clear=MagicMock()),
        rebuild_main_team=MagicMock(),
    )
    controller = AuthController(
        session,  # type: ignore[arg-type]
        SimpleNamespace(auth=SimpleNamespace(credentials_file=path)),  # type: ignore[arg-type]
        status_provider=MagicMock(),
        portal=MagicMock(),
        group_policy=MagicMock(),
        user_config_store=MagicMock(),
        server_discovery=MagicMock(),
    )
    return controller, session, path


def test_signing_out_deletes_the_saved_credentials(tmp_path) -> None:
    controller, session, path = _controller(tmp_path, ModelsConfig())

    controller.clear_cloud_credentials()

    load = CredentialsStore(path).load()
    assert not (load.ok and load.creds is not None), "a later session would sign in from them"
    session.cloud_auth.clear.assert_called_once_with()


def test_a_cloud_model_gives_way_to_one_that_has_its_own_key(tmp_path) -> None:
    models = ModelsConfig(
        default="cloud",
        registry={
            "cloud": {"model_id": "c", "api_key": CLOUD_TOKEN_SENTINEL},
            "own": {"model_id": "o", "api_key": "sk-own"},
        },
    )
    controller, session, _ = _controller(tmp_path, models)

    controller.clear_cloud_credentials()

    assert models.default == "own"
    session.rebuild_main_team.assert_called_once_with()
