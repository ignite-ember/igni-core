"""Choosing a server without a portal forgets the old server's portal.

``set_server`` with no ``portal_url`` left the previous server's portal
in the user's config (and in the running settings), so the next launch
sent sign-ins to the old portal. The session's cloud tools also kept the
old server's address.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import yaml

from igni_core.backend.server_auth import AuthController
from igni_core.core.config.user_config_store import UserConfigStore


def test_a_server_without_a_portal_drops_the_stored_one(tmp_path) -> None:
    store = UserConfigStore(tmp_path / "config.yaml")
    store.set_server("https://old.example.com", "https://portal.old.example.com")

    store.set_server("https://new.example.com")

    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert config["api_url"] == "https://new.example.com"
    assert "portal_url" not in config


def test_the_running_session_follows_the_new_server(tmp_path) -> None:
    settings = SimpleNamespace(
        api_url="https://old.example.com",
        portal_url="https://portal.old.example.com",
        auth=SimpleNamespace(credentials_file=str(tmp_path / "creds.json")),
    )
    session = SimpleNamespace(cloud_auth=MagicMock())
    controller = AuthController(
        session,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        status_provider=MagicMock(),
        portal=MagicMock(),
        group_policy=MagicMock(),
        user_config_store=UserConfigStore(tmp_path / "config.yaml"),
        server_discovery=MagicMock(),
    )

    choice = controller.set_server("https://new.example.com")

    assert not choice.error
    assert settings.portal_url == ""
    session.cloud_auth.use_server.assert_called_once_with("https://new.example.com")


def test_choosing_the_same_server_again_keeps_its_portal(tmp_path) -> None:
    """A split-port deployment's portal cannot be derived, and the web
    sign-in wall sends set_server with no portal: it erased the one in
    config.yaml (campaign R219)."""
    store = UserConfigStore(tmp_path / "config.yaml")
    store.set_server("http://localhost:7777", "http://localhost:3000")
    settings = SimpleNamespace(
        api_url="http://localhost:7777",
        portal_url="http://localhost:3000",
        auth=SimpleNamespace(credentials_file=str(tmp_path / "creds.json")),
    )
    controller = AuthController(
        SimpleNamespace(cloud_auth=MagicMock()),  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        status_provider=MagicMock(),
        portal=MagicMock(),
        group_policy=MagicMock(),
        user_config_store=store,
        server_discovery=MagicMock(),
    )

    choice = controller.set_server("http://localhost:7777/")

    assert not choice.error
    assert choice.portal_url == "http://localhost:3000"
    assert settings.portal_url == "http://localhost:3000"
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert config["portal_url"] == "http://localhost:3000"


def test_the_store_keeps_the_portal_of_the_server_it_already_has(tmp_path) -> None:
    store = UserConfigStore(tmp_path / "config.yaml")
    store.set_server("https://igni.example.com", "https://portal.example.com")

    store.set_server("https://igni.example.com")

    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert config["portal_url"] == "https://portal.example.com"
