"""What the auth controller does with a sign-in: the session learns the
credentials only after a successful login, the status it returns is the
provider's, the plan comes from the portal's answer, and a chosen server's
portal is the one the group pack asks next."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from igni_core.backend import server_auth
from igni_core.backend.schemas_rpc import LoginResult
from igni_core.backend.server_auth import AuthController
from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.auth.schemas import UserInfo, ValidateResult
from igni_core.core.config.user_config_store import UserConfigStore


class _Portal:
    def __init__(self, result: ValidateResult) -> None:
        self._result = result
        self.tokens: list[str] = []

    async def validate_token(self, token: str) -> ValidateResult:
        self.tokens.append(token)
        return self._result


def _controller(
    tmp_path, *, portal=None, token: str | None = None, config: UserConfigStore | None = None
):
    path = str(tmp_path / "credentials.json")
    CredentialsStore(path).save(Credentials(access_token="saved-token", email="ada@acme.io"))
    replaced: list[object] = []
    cloud_auth = SimpleNamespace(
        access_token=token,
        replace=replaced.append,
        use_server=MagicMock(),
    )
    status = object()
    group_policy = MagicMock()
    controller = AuthController(
        SimpleNamespace(cloud_auth=cloud_auth),  # type: ignore[arg-type]
        SimpleNamespace(  # type: ignore[arg-type]
            auth=SimpleNamespace(credentials_file=path), api_url="", portal_url=""
        ),
        status_provider=lambda: status,  # type: ignore[arg-type,return-value]
        portal=portal if portal is not None else MagicMock(),
        group_policy=group_policy,
        user_config_store=config if config is not None else UserConfigStore(tmp_path / "c.yaml"),
        server_discovery=MagicMock(),
    )
    return controller, replaced, status, group_policy


def _fake_sign_in(monkeypatch: pytest.MonkeyPatch, result: LoginResult, lines: list[str]) -> None:
    class Fake:
        def __init__(self, portal, store, group_policy) -> None:
            pass

        async def login(self, on_status):
            for line in lines:
                on_status(line)
            return result

    monkeypatch.setattr(server_auth, "BrowserSignIn", Fake)


# ── login ───────────────────────────────────────────────────────────


async def test_a_successful_login_gives_the_session_the_credentials_just_written(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, replaced, _, _ = _controller(tmp_path)
    _fake_sign_in(monkeypatch, LoginResult(ok=True, email="ada@acme.io"), ["Opening browser"])
    heard: list[str] = []

    result = await controller.login(heard.append)

    assert result.ok and result.email == "ada@acme.io"
    assert heard == ["Opening browser"]
    [creds] = replaced
    assert creds.access_token == "saved-token"  # type: ignore[attr-defined]


async def test_a_failed_login_leaves_the_session_as_it_was(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, replaced, _, _ = _controller(tmp_path)
    _fake_sign_in(monkeypatch, LoginResult(ok=False, error="timeout"), [])

    result = await controller.login()

    assert (result.ok, result.error) == (False, "timeout")
    assert replaced == []


def test_reloading_credentials_answers_with_the_fresh_status(tmp_path) -> None:
    controller, replaced, status, _ = _controller(tmp_path)

    assert controller.reload_cloud_credentials() is status
    assert len(replaced) == 1


# ── the plan ────────────────────────────────────────────────────────


async def test_there_is_no_plan_when_nobody_is_signed_in(tmp_path) -> None:
    portal = _Portal(ValidateResult(ok=True, user=UserInfo(email="a@b.c")))
    controller, _, _, _ = _controller(tmp_path, portal=portal, token=None)

    assert await controller.get_cloud_plan() is None
    assert portal.tokens == []


async def test_there_is_no_plan_when_the_portal_refuses_the_token(tmp_path) -> None:
    portal = _Portal(ValidateResult(ok=False, reason="http_error", user=UserInfo(email="a@b.c")))
    controller, _, _, _ = _controller(tmp_path, portal=portal, token="tok")

    assert await controller.get_cloud_plan() is None


async def test_there_is_no_plan_when_the_portal_answers_without_a_user(tmp_path) -> None:
    portal = _Portal(ValidateResult(ok=True, user=None))
    controller, _, _, _ = _controller(tmp_path, portal=portal, token="tok")

    assert await controller.get_cloud_plan() is None


async def test_the_plan_is_the_signed_in_users_account(tmp_path) -> None:
    user = UserInfo(email="ada@acme.io", full_name="Ada L", org_display_name="Acme")
    portal = _Portal(ValidateResult(ok=True, user=user))
    controller, _, _, _ = _controller(tmp_path, portal=portal, token="tok")

    plan = await controller.get_cloud_plan()

    assert portal.tokens == ["tok"]
    assert plan is not None
    assert (plan.org_name, plan.email, plan.full_name) == ("Acme", "ada@acme.io", "Ada L")


# ── choosing a server ───────────────────────────────────────────────


def test_the_group_pack_asks_the_chosen_servers_portal_next(tmp_path) -> None:
    controller, _, _, group_policy = _controller(tmp_path)

    controller.set_server("https://igni.acme.example/", "https://portal.acme.example/")

    [(portal,), _] = group_policy.use_portal.call_args
    assert portal is controller._portal
    assert controller._portal._api_url == "https://igni.acme.example"


def test_a_plaintext_portal_alone_refuses_the_choice_and_writes_nothing(tmp_path) -> None:
    store = UserConfigStore(tmp_path / "c.yaml")
    controller, _, _, group_policy = _controller(tmp_path, config=store)

    answer = controller.set_server("https://igni.acme.example", "http://portal.acme.example")

    assert answer.error == "http://portal.acme.example is not an https address."
    assert store.read() == {}
    assert controller._settings.api_url == ""
    group_policy.use_portal.assert_not_called()


def test_nothing_is_changed_when_the_address_is_missing(tmp_path) -> None:
    controller, _, _, group_policy = _controller(tmp_path)

    answer = controller.set_server(None)  # type: ignore[arg-type]

    assert answer.error == "Name the server to connect to."
    group_policy.use_portal.assert_not_called()
