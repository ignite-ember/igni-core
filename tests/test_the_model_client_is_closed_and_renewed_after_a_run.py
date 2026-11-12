"""After a run the model's HTTP client is closed and a working one installed.

:meth:`ModelHttpClientManager.close_and_replace` never raises and always
leaves the model a client the next run can use. What is pinned here is
what ``tests/test_backend_server.py`` and
``tests/test_the_model_carries_the_current_cloud_token.py`` leave open:
the result a close failure or a hang reports, that the replacement is
installed regardless, that injected limits reach it, and that a Claude
model's ``httpx2`` client is renewed as an ``httpx2`` one with its auth.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import httpx2

from igni_core.backend.model_http_client_manager import (
    DEFAULT_LIMITS,
    ModelHttpClientManager,
)
from igni_core.core.activity.http import BusyAsyncClient
from igni_core.core.activity.http2 import BusyAsyncClient as BusyAsyncClient2


def _team(client: Any) -> Any:
    return SimpleNamespace(model=SimpleNamespace(http_client=client))


class _Refuses(httpx.AsyncClient):
    async def aclose(self) -> None:
        raise RuntimeError("socket already gone")


class _Hangs(httpx.AsyncClient):
    async def aclose(self) -> None:
        await asyncio.sleep(60)


async def test_a_clean_close_reports_ok_and_installs_igni_s_own_client():
    old = httpx.AsyncClient()
    team = _team(old)

    result = await ModelHttpClientManager().close_and_replace(team)

    assert (result.ok, result.reason) == (True, "")
    assert old.is_closed
    assert isinstance(team.model.http_client, BusyAsyncClient)


async def test_a_close_that_fails_says_why_and_the_next_run_still_has_a_client():
    team = _team(_Refuses())

    result = await ModelHttpClientManager().close_and_replace(team)

    assert result.ok is False
    assert result.reason == "socket already gone"
    assert isinstance(team.model.http_client, BusyAsyncClient)
    assert not team.model.http_client.is_closed


async def test_a_close_that_hangs_is_given_up_on_after_three_seconds(monkeypatch):
    waited: list[float] = []
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable: Any, timeout: float) -> Any:
        waited.append(timeout)
        return await real_wait_for(awaitable, timeout=0.05)

    monkeypatch.setattr(asyncio, "wait_for", short_wait_for)
    team = _team(_Hangs())

    result = await ModelHttpClientManager().close_and_replace(team)

    assert waited == [3]
    assert result.ok is False
    assert isinstance(team.model.http_client, BusyAsyncClient)


async def test_a_team_with_no_model_is_ok_and_gets_nothing_installed():
    team = SimpleNamespace()

    result = await ModelHttpClientManager().close_and_replace(team)  # type: ignore[arg-type]

    assert result.ok is True
    assert not hasattr(team, "model")


async def test_a_model_with_no_client_yet_is_given_one():
    team = _team(None)

    result = await ModelHttpClientManager().close_and_replace(team)

    assert result.ok is True
    assert isinstance(team.model.http_client, BusyAsyncClient)


async def test_a_client_that_is_not_async_is_left_open_and_replaced_by_a_bare_one():
    sync = httpx.Client()
    team = _team(sync)

    result = await ModelHttpClientManager().close_and_replace(team)

    assert result.ok is True
    assert not sync.is_closed
    assert isinstance(team.model.http_client, BusyAsyncClient)
    sync.close()


async def test_the_limits_given_at_construction_reach_the_fresh_client():
    limits = httpx.Limits(max_connections=3, max_keepalive_connections=1, keepalive_expiry=7)
    team = _team(httpx.AsyncClient())

    await ModelHttpClientManager(limits=limits).close_and_replace(team)

    pool = team.model.http_client._transport._pool
    assert (pool._max_connections, pool._max_keepalive_connections) == (3, 1)


def test_without_limits_given_the_modest_defaults_apply():
    assert (
        DEFAULT_LIMITS.max_connections,
        DEFAULT_LIMITS.max_keepalive_connections,
        DEFAULT_LIMITS.keepalive_expiry,
    ) == (10, 5, 30)


async def test_the_fresh_client_keeps_the_old_one_s_auth_and_timeout():
    auth = httpx.BasicAuth("ada", "pw")
    old = httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(12.5))
    team = _team(old)

    await ModelHttpClientManager().close_and_replace(team)

    assert team.model.http_client.auth is auth
    assert team.model.http_client.timeout == httpx.Timeout(12.5)


async def test_a_claude_models_httpx2_client_is_renewed_as_an_httpx2_client():
    auth = httpx2.BasicAuth("ada", "pw")
    old = BusyAsyncClient2(busy_holder="model", auth=auth, timeout=httpx2.Timeout(9.0))
    team = _team(old)
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=11)

    result = await ModelHttpClientManager(limits=limits).close_and_replace(team)

    new = team.model.http_client
    assert result.ok is True
    assert old.is_closed
    assert isinstance(new, BusyAsyncClient2), "an httpx client there would be refused by the SDK"
    assert new is not old
    assert new.auth is auth
    assert new.timeout == httpx2.Timeout(9.0)
    pool = new._transport._pool
    assert (pool._max_connections, pool._max_keepalive_connections) == (4, 2)
