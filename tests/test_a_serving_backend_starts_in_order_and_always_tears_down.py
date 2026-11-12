"""A booted backend waits for a window, starts what needs one, serves, and always tears down.

``ServingBackend.serve`` is driven over recording stand-ins for each part,
so the order of the starts, the order of the stops, the early shutdown, a
failing start and a raising stop are pinned. The real backend, window and
signal are exercised end to end by the boot tests.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.serving_backend import BackendParts, ServingBackend

STARTS = [
    "startup.start",
    "notifications.start",
    "ingress.start",
    "health.start",
    "host.start_signals",
    "orchestrator.serve",
]
STOPS = [
    "ingress.stop",
    "notifications.stop",
    "host.stop",
    "health.stop",
    "startup.stop",
    "origins.stop_all",
    "supervisor.teardown",
]


class Rig:
    def __init__(self, *, shutdown_first: bool = False) -> None:
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.waited: list[Any] = []
        self.shutdown_event = asyncio.Event()
        self.shutdown_first = shutdown_first
        self.never = asyncio.Event()

    def step(self, name: str, *, is_async: bool = True) -> Any:
        async def run(*_: Any) -> None:
            self.calls.append(name)
            if name in self.fail:
                raise self.fail[name]

        def run_sync(*_: Any) -> None:
            self.calls.append(name)
            if name in self.fail:
                raise self.fail[name]

        return run if is_async else run_sync

    def parts(self, *, no_timeout: bool = True) -> BackendParts:
        rig = self

        async def wait_for_connection(timeout: float | None = 30.0) -> None:
            rig.waited.append(timeout)
            if rig.shutdown_first:
                await rig.never.wait()

        async def wait_for_shutdown() -> None:
            if rig.shutdown_first:
                return
            await rig.never.wait()

        transport = SimpleNamespace(wait_for_connection=wait_for_connection)
        supervisor = SimpleNamespace(
            wait_for_shutdown=wait_for_shutdown,
            shutdown_event=self.shutdown_event,
            teardown=self.step("supervisor.teardown"),
        )
        return BackendParts.model_construct(
            supervisor=supervisor,
            orchestrator=SimpleNamespace(serve=self._serve),
            transport=transport,
            no_timeout=no_timeout,
            notifications=SimpleNamespace(
                start=self.step("notifications.start", is_async=False),
                stop=self.step("notifications.stop"),
            ),
            ingress=SimpleNamespace(
                start=self.step("ingress.start", is_async=False), stop=self.step("ingress.stop")
            ),
            extensions=SimpleNamespace(
                health=SimpleNamespace(
                    start=self.step("health.start", is_async=False),
                    stop=self.step("health.stop"),
                ),
                origins=SimpleNamespace(stop_all=self.step("origins.stop_all")),
            ),
            startup=SimpleNamespace(
                start=self.step("startup.start"), stop=self.step("startup.stop")
            ),
            host=SimpleNamespace(
                start_signals=self.step("host.start_signals"), stop=self.step("host.stop")
            ),
        )

    async def _serve(self, event: asyncio.Event) -> None:
        assert event is self.shutdown_event, "served until the supervisor's shutdown event"
        self.calls.append("orchestrator.serve")
        if "orchestrator.serve" in self.fail:
            raise self.fail["orchestrator.serve"]


async def test_a_window_arrives_the_parts_start_in_order_and_everything_stops_in_order() -> None:
    rig = Rig()

    await ServingBackend(rig.parts()).serve()

    assert rig.calls == STARTS + STOPS
    assert rig.waited == [None], "a WebSocket backend waits for its window without a timeout"


async def test_a_backend_with_a_timeout_waits_with_the_default_one() -> None:
    rig = Rig()

    await ServingBackend(rig.parts(no_timeout=False)).serve()

    assert rig.waited == [30.0]


async def test_a_shutdown_before_any_window_starts_nothing_but_still_tears_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(shutdown_first=True)

    with caplog.at_level(logging.INFO, logger="igni_core.backend.serving_backend"):
        await ServingBackend(rig.parts()).serve()

    assert rig.calls == STOPS
    assert "Shutdown requested before any window connected" in caplog.text


@pytest.mark.parametrize("failing", STARTS)
async def test_a_start_that_fails_is_logged_and_the_teardown_still_runs(
    failing: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig()
    rig.fail[failing] = RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="igni_core.backend.serving_backend"):
        await ServingBackend(rig.parts()).serve()

    started = STARTS[: STARTS.index(failing) + 1]
    assert rig.calls == started + STOPS, "nothing after the failure started"
    assert "Backend error: boom" in caplog.text


async def test_a_stop_that_raises_does_not_skip_the_rest_and_is_seen_by_the_caller() -> None:
    rig = Rig()
    rig.fail["host.stop"] = RuntimeError("stop failed")

    with pytest.raises(RuntimeError, match="stop failed"):
        await ServingBackend(rig.parts()).serve()

    assert rig.calls == STARTS + STOPS, "the supervisor's teardown still ran"
