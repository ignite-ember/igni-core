"""The login coordinator: one sign-in at a time, each followed by its flow id.

``start`` acks at once with a flow id; the status pushes and the final
result carry it so a window follows its own sign-in. A new start cancels
the one in flight, a cancelled sign-in pushes no result, and listeners
run only after a successful one.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.schemas_rpc import LoginResult


class Push:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []
        self.results: list[tuple[bool, str, str]] = []

    async def on_login_status(self, text: str, *, flow_id: str = "") -> None:
        self.statuses.append((text, flow_id))

    async def on_login_result(self, *, success: bool, result: str, flow_id: str = "") -> None:
        self.results.append((success, result, flow_id))


class Auth:
    """The auth controller: a login that can be held, failed or made to raise."""

    def __init__(self, result: LoginResult | Exception | None = None) -> None:
        self.result = result if result is not None else LoginResult(ok=True, email="p@example.com")
        self.release: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.calls = 0
        self.events: list[str] = []

    async def login(self, on_status: Any = None) -> LoginResult:
        self.calls += 1
        self.events.append("login")
        self.started.set()
        if on_status is not None:
            await on_status("opening the browser")
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def reload_cloud_credentials(self) -> None:
        self.events.append("reload")


def _coordinator(auth: Auth) -> tuple[LoginCoordinator, Push]:
    push = Push()
    backend = SimpleNamespace(controllers=SimpleNamespace(auth=auth))
    return LoginCoordinator(backend=backend, push_bridge=push), push  # type: ignore[arg-type]


async def _settle(login: LoginCoordinator) -> None:
    task = login._task
    assert task is not None
    await asyncio.wait_for(task, 2)


async def test_start_acks_at_once_with_a_flow_id_before_the_login_is_done() -> None:
    auth = Auth()
    auth.release = asyncio.Event()
    login, push = _coordinator(auth)

    started = await login.start()

    assert started.started is True
    assert re.fullmatch(r"login-[0-9a-f]{12}", started.flow_id)
    assert push.results == []
    auth.release.set()
    await _settle(login)


async def test_two_sign_ins_get_distinct_flow_ids() -> None:
    login, _ = _coordinator(Auth())

    first = await login.start()
    await _settle(login)
    second = await login.start()
    await _settle(login)

    assert first.flow_id != second.flow_id


async def test_a_successful_sign_in_reloads_credentials_then_tells_listeners_then_pushes() -> None:
    auth = Auth()
    login, push = _coordinator(auth)
    login.on_signed_in(lambda: auth.events.append("listener-1"))
    login.on_signed_in(lambda: auth.events.append("listener-2"))

    started = await login.start()
    await _settle(login)

    assert auth.events == ["login", "reload", "listener-1", "listener-2"]
    assert push.statuses == [("opening the browser", started.flow_id)]
    assert push.results == [(True, "p@example.com", started.flow_id)]


async def test_a_failed_sign_in_pushes_the_reason_and_runs_no_listener() -> None:
    auth = Auth(LoginResult(ok=False, error="denied"))
    login, push = _coordinator(auth)
    seen: list[str] = []
    login.on_signed_in(lambda: seen.append("listener"))

    started = await login.start()
    await _settle(login)

    assert push.results == [(False, "denied", started.flow_id)]
    assert seen == []
    assert "reload" not in auth.events


async def test_a_listener_that_raises_neither_undoes_the_sign_in_nor_stops_the_others() -> None:
    auth = Auth()
    login, push = _coordinator(auth)
    seen: list[str] = []

    def broken() -> None:
        raise RuntimeError("listener broke")

    login.on_signed_in(broken)
    login.on_signed_in(lambda: seen.append("second"))

    started = await login.start()
    await _settle(login)

    assert seen == ["second"]
    assert push.results == [(True, "p@example.com", started.flow_id)]


async def test_a_login_that_raises_pushes_a_failure_with_the_error_text() -> None:
    login, push = _coordinator(Auth(RuntimeError("network down")))

    started = await login.start()
    await _settle(login)

    assert push.results == [(False, "network down", started.flow_id)]


async def test_a_new_start_cancels_the_login_in_flight_and_only_the_new_one_answers() -> None:
    auth = Auth()
    auth.release = asyncio.Event()
    login, push = _coordinator(auth)

    first = await login.start()
    await asyncio.wait_for(auth.started.wait(), 2)
    first_task = login._task
    restart = asyncio.ensure_future(login.start())
    done, _ = await asyncio.wait({restart}, timeout=2)
    assert done, "the first login was not cancelled, so the restart waits on it"
    second = restart.result()
    assert first_task is not None and first_task.done()
    auth.release.set()
    await _settle(login)

    assert auth.calls == 2
    assert push.results == [(True, "p@example.com", second.flow_id)]
    assert first.flow_id not in {flow for _, _, flow in push.results}


async def test_a_cancelled_sign_in_pushes_no_result() -> None:
    auth = Auth()
    auth.release = asyncio.Event()
    login, push = _coordinator(auth)
    seen: list[str] = []
    login.on_signed_in(lambda: seen.append("listener"))

    await login.start()
    await asyncio.wait_for(auth.started.wait(), 2)
    await asyncio.wait_for(login.cancel(), 2)

    assert login._task is not None and login._task.done()
    assert push.results == [] and seen == []


async def test_cancel_with_nothing_in_flight_or_after_it_finished_does_nothing() -> None:
    login, push = _coordinator(Auth())

    await login.cancel()
    started = await login.start()
    await _settle(login)
    await login.cancel()

    assert push.results == [(True, "p@example.com", started.flow_id)]


async def test_a_sign_in_without_an_email_pushes_an_empty_result() -> None:
    login, push = _coordinator(Auth(LoginResult(ok=True)))

    started = await login.start()
    await _settle(login)

    assert push.results == [(True, "", started.flow_id)]
