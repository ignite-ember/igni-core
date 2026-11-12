"""The sign-in RPCs hand the call to the right collaborator and say what
it answered.

``login_start`` answers with what the login coordinator started,
``reload_creds`` and ``clear_creds`` answer with the session's status,
and ``clear_creds`` also tells the notification center the person signed
out, so their notifications leave the badges, even when that center is
missing (a headless backend) or fails (the sign-out already happened).
The calls' published streams, correlation and duration are what a client
follows login and sign-out by.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.rpc_binding import Correlation, RpcArgsError, RpcBinding
from igni_core.backend.rpc_handlers.auth import AuthRpcHandler
from igni_core.backend.schemas_auth import ServerChoice, ServerFound
from igni_core.backend.schemas_rpc import LoginStarted
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


def _handler(notifications: object | None = None) -> tuple[AuthRpcHandler, MagicMock]:
    ctx = MagicMock()
    ctx.notifications = notifications
    return AuthRpcHandler(ctx), ctx


def _binding(method: RpcMethod) -> RpcBinding:
    return RpcBinding.on(AuthRpcHandler)[method][1]


async def test_login_start_answers_what_the_coordinator_started() -> None:
    handler, ctx = _handler()
    ctx.login.start = AsyncMock(return_value=LoginStarted(started=True, flow_id="f-1"))

    answer = await handler.methods()[RpcMethod.LOGIN]({})

    assert answer == LoginStarted(started=True, flow_id="f-1")
    ctx.login.start.assert_awaited_once_with()


def test_reload_creds_answers_the_status_after_reading_the_sign_in_again() -> None:
    handler, ctx = _handler()
    status = MagicMock(name="status")
    ctx.backend.controllers.auth.reload_cloud_credentials.return_value = status

    assert handler.methods()[RpcMethod.RELOAD_CLOUD_CREDENTIALS]({}) is status


async def test_clear_creds_signs_out_then_tells_the_notification_center() -> None:
    calls: list[str] = []
    center = MagicMock()
    center.signed_out = AsyncMock(side_effect=lambda: calls.append("center told"))
    handler, ctx = _handler(center)
    status = MagicMock(name="status")
    auth = ctx.backend.controllers.auth
    auth.clear_cloud_credentials.side_effect = lambda: (calls.append("signed out"), status)[1]

    answer = await handler.methods()[RpcMethod.CLEAR_CLOUD_CREDENTIALS]({})

    assert answer is status
    assert calls == ["signed out", "center told"]


async def test_clear_creds_without_a_notification_center_still_signs_out() -> None:
    handler, ctx = _handler(None)
    status = MagicMock(name="status")
    ctx.backend.controllers.auth.clear_cloud_credentials.return_value = status

    answer = await handler.methods()[RpcMethod.CLEAR_CLOUD_CREDENTIALS]({})

    assert answer is status
    ctx.backend.controllers.auth.clear_cloud_credentials.assert_called_once_with()


async def test_a_notification_center_that_fails_does_not_undo_the_sign_out() -> None:
    center = MagicMock()
    center.signed_out = AsyncMock(side_effect=RuntimeError("follower would not stop"))
    handler, ctx = _handler(center)
    status = MagicMock(name="status")
    ctx.backend.controllers.auth.clear_cloud_credentials.return_value = status

    answer = await handler.methods()[RpcMethod.CLEAR_CLOUD_CREDENTIALS]({})

    assert answer is status


async def test_discover_server_asks_about_the_email_given() -> None:
    handler, ctx = _handler()
    found = ServerFound(
        api_url="https://api.acme.io", portal_url="https://acme.io", source="well-known"
    )
    ctx.backend.controllers.auth.discover_server = AsyncMock(return_value=found)

    answer = await handler.methods()[RpcMethod.DISCOVER_SERVER]({"email": "ada@acme.io"})

    assert answer == found
    ctx.backend.controllers.auth.discover_server.assert_awaited_once_with("ada@acme.io")


def test_discover_server_without_an_email_is_refused() -> None:
    handler, ctx = _handler()
    with pytest.raises(RpcArgsError, match="email"):
        handler.methods()[RpcMethod.DISCOVER_SERVER]({})


def test_set_server_hands_over_the_api_url_then_the_portal_url() -> None:
    handler, ctx = _handler()
    choice = ServerChoice(api_url="https://api.acme.io", portal_url="https://portal.acme.io")
    ctx.backend.controllers.auth.set_server.return_value = choice

    answer = handler.methods()[RpcMethod.SET_SERVER](
        {"api_url": "https://api.acme.io", "portal_url": "https://portal.acme.io"}
    )

    assert answer == choice
    ctx.backend.controllers.auth.set_server.assert_called_once_with(
        "https://api.acme.io", "https://portal.acme.io"
    )


def test_set_server_without_an_api_url_is_refused() -> None:
    handler, ctx = _handler()
    with pytest.raises(RpcArgsError, match="api_url"):
        handler.methods()[RpcMethod.SET_SERVER]({"portal_url": "https://x.io"})
    ctx.backend.controllers.auth.set_server.assert_not_called()


def test_login_is_followed_by_its_pushes_and_correlated_by_flow_id() -> None:
    binding = _binding(RpcMethod.LOGIN)

    assert binding.streams == (
        PushChannel.LOGIN_STATUS,
        PushChannel.LOGIN_RESULT,
        PushChannel.NOTIFICATIONS_CHANGED,
    )
    assert binding.correlate == Correlation(by="payload.flow_id", source="result.flow_id")
    assert binding.scope is RpcScope.IGNI


def test_sign_out_announces_notifications_changed_with_the_signed_out_cause() -> None:
    binding = _binding(RpcMethod.CLEAR_CLOUD_CREDENTIALS)

    assert binding.streams == (PushChannel.NOTIFICATIONS_CHANGED,)
    assert binding.correlate == Correlation(by="payload.cause", equals="signed_out")


@pytest.mark.parametrize(
    ("method", "duration"),
    [
        (RpcMethod.LOGIN, RpcDuration.INSTANT),
        (RpcMethod.RELOAD_CLOUD_CREDENTIALS, RpcDuration.IO),
        (RpcMethod.CLEAR_CLOUD_CREDENTIALS, RpcDuration.IO),
        (RpcMethod.GET_CLOUD_PLAN, RpcDuration.NETWORK),
        (RpcMethod.DISCOVER_SERVER, RpcDuration.NETWORK),
        (RpcMethod.SET_SERVER, RpcDuration.IO),
    ],
)
def test_each_auth_call_says_how_long_it_takes(method: RpcMethod, duration: RpcDuration) -> None:
    assert _binding(method).duration is duration
