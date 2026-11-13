"""The rules the health monitor keeps when a service does not answer.

A service that is gone is restarted, but not forever: one attempt a
minute, and whatever it said first is what the person is told. These run
against a manager that answers as scripted, because what is being pinned
is what the monitor records and when it tries again, not the subprocess
(``test_a_dead_extension_stops_claiming_to_be_ready`` kills a real one).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.extensions import health as health_module
from igni_core.extensions.health import ExtensionHealth
from igni_core.extensions.host.processes import ExtensionProcesses
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.service import McpExtensionService

NOT_CONNECTED = "its service is not connected."


def manifest(extension_id: str) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": extension_id,
            "trust": "unsigned",
            "dev": {"command": ["true"]},
        }
    )


class Session:
    def __init__(self, listing: Any) -> None:
        self._listing = listing

    async def list_tools(self) -> Any:
        return await self._listing()


class Client:
    def __init__(self, listing: Any) -> None:
        self._session = Session(listing)

    async def get_session_for_run(self) -> Session:
        return self._session


class Mcp:
    """Answers as scripted: a client or none, a connect outcome."""

    def __init__(self) -> None:
        self.client: Client | None = None
        self.connect_result: Any = SimpleNamespace(ok=True, reason="")
        self.connect_raises: Exception | None = None
        self.connect_hangs = False
        self.calls: list[str] = []

    def get_client(self, name: str) -> Client | None:
        return self.client

    async def disconnect_one(self, name: str) -> bool:
        self.calls.append(f"disconnect {name}")
        return True

    async def connect(self, name: str) -> Any:
        self.calls.append(f"connect {name}")
        if self.connect_hangs:
            await asyncio.sleep(3600)
        if self.connect_raises is not None:
            raise self.connect_raises
        return self.connect_result


class Rpc(ExtensionRpc):
    def __init__(self, registry: ExtensionRegistry) -> None:
        super().__init__(registry)
        self.bound: dict[str, Any] = {}

    def bind(self, extension_id: str, service: Any) -> None:
        self.bound[extension_id] = service
        super().bind(extension_id, service)


class Wired:
    def __init__(self) -> None:
        self.registry = ExtensionRegistry()
        self.registry.install(manifest("acme.one"), state=ExtensionState.READY)
        self.rpc = Rpc(self.registry)
        self.mcp = Mcp()
        self.health = ExtensionHealth(
            registry=self.registry,
            rpc=self.rpc,
            mcp=self.mcp,  # type: ignore[arg-type]
            interval_s=0.01,
            processes=ExtensionProcesses(),
        )

    @property
    def entry(self):
        entry = self.registry.get("acme.one")
        assert entry is not None
        return entry


@pytest.fixture
def wired() -> Wired:
    return Wired()


async def test_a_second_restart_inside_a_minute_is_not_attempted(wired: Wired) -> None:
    assert await wired.health.probe("acme.one") is ExtensionState.READY
    assert wired.mcp.calls == ["disconnect ext:acme.one", "connect ext:acme.one"]

    wired.mcp.client = None
    assert await wired.health.probe("acme.one") is ExtensionState.FAILED

    assert wired.mcp.calls == ["disconnect ext:acme.one", "connect ext:acme.one"]
    assert wired.entry.state is ExtensionState.FAILED


async def test_a_restart_is_allowed_again_after_the_backoff(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    await wired.health.probe("acme.one")
    monkeypatch.setattr(health_module, "RESTART_BACKOFF_S", 0.0)

    await wired.health.probe("acme.one")

    assert wired.mcp.calls.count("connect ext:acme.one") == 2


async def test_a_service_that_hangs_is_failed_with_how_long_it_was_given(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang() -> Any:
        await asyncio.sleep(3600)

    wired.mcp.client = Client(hang)
    wired.mcp.connect_result = SimpleNamespace(ok=False, reason="still down")
    monkeypatch.setattr(health_module, "PROBE_TIMEOUT_S", 1.0)

    assert await wired.health.probe("acme.one") is ExtensionState.FAILED

    assert wired.entry.reason.startswith("its service did not answer within 1s.")


async def test_a_service_that_raises_on_listing_is_failed_with_the_error(wired: Wired) -> None:
    async def broken() -> Any:
        raise RuntimeError("pipe closed")

    wired.mcp.client = Client(broken)
    wired.mcp.connect_result = SimpleNamespace(ok=False, reason="still down")

    await wired.health.probe("acme.one")

    assert wired.entry.reason.startswith("its service stopped answering: pipe closed")


async def test_a_restart_that_raises_is_recorded_after_the_first_cause(wired: Wired) -> None:
    wired.mcp.connect_raises = OSError("no such file")

    assert await wired.health.probe("acme.one") is ExtensionState.FAILED

    assert wired.entry.reason == (
        f"{NOT_CONNECTED} (then: its service could not be restarted: no such file)"
    )


async def test_a_restart_that_never_comes_up_is_given_up_on(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired.mcp.connect_hangs = True
    monkeypatch.setattr(health_module, "RESTART_TIMEOUT_S", 0.05)

    assert await wired.health.probe("acme.one") is ExtensionState.FAILED

    assert "its service could not be restarted" in wired.entry.reason


async def test_a_restart_that_connects_not_ok_says_why_or_says_it_has_no_reason(
    wired: Wired,
) -> None:
    wired.mcp.connect_result = SimpleNamespace(ok=False, reason="port taken")
    await wired.health.probe("acme.one")
    assert wired.entry.reason == f"{NOT_CONNECTED} (then: its service did not restart: port taken)"

    other = Wired()
    other.mcp.connect_result = SimpleNamespace(ok=False, reason="")
    await other.health.probe("acme.one")
    assert other.entry.reason.endswith("(then: its service did not restart: no reason given)")


async def test_a_reason_already_in_the_first_cause_is_not_repeated(wired: Wired) -> None:
    wired.mcp.connect_result = SimpleNamespace(ok=False, reason="x")
    await wired.health.probe("acme.one")
    assert wired.entry.reason == f"{NOT_CONNECTED} (then: its service did not restart: x)"

    # Inside the backoff the probe finds the same thing again, and says it once.
    await wired.health.probe("acme.one")

    assert wired.entry.reason == NOT_CONNECTED


async def test_a_restart_that_works_binds_the_service_and_forgets_the_cause(
    wired: Wired,
) -> None:
    assert await wired.health.probe("acme.one") is ExtensionState.READY

    assert isinstance(wired.rpc.bound["acme.one"], McpExtensionService)
    assert wired.entry.state is ExtensionState.READY
    assert wired.entry.reason == ""

    # The cause was forgotten: a new failure starts a new first cause.
    wired.mcp.connect_result = SimpleNamespace(ok=False, reason="later")
    wired.health._last_restart.clear()  # noqa: SLF001
    await wired.health.probe("acme.one")
    assert wired.entry.reason == f"{NOT_CONNECTED} (then: its service did not restart: later)"


async def test_an_answering_service_clears_a_failed_state(wired: Wired) -> None:
    async def listing() -> list[str]:
        return []

    wired.registry.set_state("acme.one", ExtensionState.FAILED, "its service exited at start-up")
    wired.mcp.client = Client(listing)

    assert await wired.health.probe("acme.one") is ExtensionState.READY

    assert wired.entry.state is ExtensionState.READY
    assert wired.entry.reason == ""
    assert wired.mcp.calls == []


async def test_a_failed_extension_keeps_its_own_reason_in_front(wired: Wired) -> None:
    wired.registry.set_state("acme.one", ExtensionState.FAILED, "config file missing")
    wired.mcp.connect_result = SimpleNamespace(ok=False, reason="gone")

    await wired.health.probe("acme.one")

    assert wired.entry.reason.startswith("config file missing (then: ")


async def test_two_probes_of_one_extension_never_overlap(wired: Wired) -> None:
    running = 0
    most = 0

    async def listing() -> list[str]:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.02)
        running -= 1
        return []

    wired.mcp.client = Client(listing)

    await asyncio.gather(wired.health.probe("acme.one"), wired.health.probe("acme.one"))

    assert most == 1


async def test_a_sweep_that_raises_does_not_end_the_loop(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweeps = 0

    async def probe_all() -> None:
        nonlocal sweeps
        sweeps += 1
        if sweeps == 1:
            raise RuntimeError("registry blew up")

    monkeypatch.setattr(wired.health, "probe_all", probe_all)
    wired.health.start()
    try:
        for _ in range(200):
            if sweeps >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await wired.health.stop()

    assert sweeps >= 3


async def test_starting_twice_runs_one_loop_and_stop_ends_it(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweeps = 0

    async def probe_all() -> None:
        nonlocal sweeps
        sweeps += 1

    monkeypatch.setattr(wired.health, "probe_all", probe_all)
    wired.health.start()
    wired.health.start()
    await asyncio.sleep(0.105)
    await wired.health.stop()
    seen = sweeps
    await asyncio.sleep(0.05)

    assert sweeps == seen
    assert seen <= 11  # one loop at a 10ms interval; two would near double it


async def test_an_unknown_extension_is_ignored_by_both_triggers(wired: Wired) -> None:
    wired.health.restart_faulty("acme.nobody", "hung")
    wired.health.on_call_failed("acme.nobody")

    assert wired.health.pending_probes == 0
    assert wired.mcp.calls == []
    assert await wired.health.probe("acme.nobody") is None


async def test_a_failed_call_probes_without_making_the_caller_wait(wired: Wired) -> None:
    async def listing() -> list[str]:
        await asyncio.sleep(0.05)
        return []

    wired.registry.set_state("acme.one", ExtensionState.FAILED, "was down")
    wired.mcp.client = Client(listing)

    wired.health.on_call_failed("acme.one")

    assert wired.entry.state is ExtensionState.FAILED
    assert wired.health.pending_probes == 1
    while wired.health.pending_probes:
        await asyncio.sleep(0.01)
    assert wired.entry.state is ExtensionState.READY


async def test_a_restart_that_errors_is_logged_not_raised(
    wired: Wired, caplog: pytest.LogCaptureFixture
) -> None:
    class Boom(ExtensionProcesses):
        def kill(self, extension_id: str) -> bool:
            raise RuntimeError("no such group")

    wired.health._processes = Boom()  # noqa: SLF001
    wired.health.restart_faulty("acme.one", "hung")
    await asyncio.sleep(0)
    while wired.health.pending_probes:
        await asyncio.sleep(0.01)

    assert "restart of acme.one failed" in caplog.text


async def test_two_monitors_do_not_share_their_in_flight_probes() -> None:
    first, second = Wired(), Wired()

    async def listing() -> list[str]:
        await asyncio.sleep(0.05)
        return []

    first.mcp.client = Client(listing)
    first.health.on_call_failed("acme.one")

    assert first.health.pending_probes == 1
    assert second.health.pending_probes == 0
    await first.health.stop()
    assert first.health.pending_probes == 0
