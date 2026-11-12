"""The host model runtime: the model, its channel and the project's git signals.

``HostModelRuntime.build`` composes them over the extensions runtime and
the process-wide hubs; ``start_signals`` starts the workspace poll after
the installs; ``stop`` closes the channel before the signals.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.host_model_runtime import HostModelRuntime
from igni_core.backend.observed_transport import ObservedTransport
from igni_core.core.activity import ActivityRegistry
from igni_core.core.activity.registry import BusyKind
from igni_core.core.approvals.events import ApprovalChange, ApprovalState, approval_events
from igni_core.core.hooks.extension_gate import extension_hook_gate
from igni_core.extensions.host.channel import HostChannel
from igni_core.extensions.host.hooks import ExtensionHooks
from igni_core.extensions.host.model import HostModel
from igni_core.extensions.host.processes import ExtensionProcesses
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.store import ExtensionStore
from tests.host_model_helpers import CapturingTransport, Sink, manifest


@pytest.fixture(autouse=True)
def _the_process_wide_hubs_as_they_were() -> Iterator[None]:
    """``build`` listens on the process's own hubs: leave them as found."""
    try:
        yield
    finally:
        approval_events.clear()
        ActivityRegistry.reset_shared()
        extension_hook_gate.install(None)


class _Orchestrator:
    def __init__(self) -> None:
        self.client_state = SimpleNamespace()
        self.pool = SimpleNamespace(runtimes=[])

    def mount(self, rpc: Any) -> None:
        return None

    def watch_inbound(self, watcher: Any) -> None:
        return None


def _build(tmp_path: Path) -> tuple[HostModelRuntime, Any]:
    registry = ExtensionRegistry()
    extensions = SimpleNamespace(
        registry=registry,
        hooks=ExtensionHooks(registry=registry),
        processes=ExtensionProcesses(),
    )
    built = HostModelRuntime.build(
        orchestrator=_Orchestrator(),  # type: ignore[arg-type]
        transport=ObservedTransport(CapturingTransport()),
        runtime=extensions,  # type: ignore[arg-type]
        project_dir=tmp_path,
        store=ExtensionStore(),
        notices=None,  # type: ignore[arg-type]
    )
    return built, extensions


def _listen(built: HostModelRuntime, extensions: Any, *wires: str) -> Sink:
    extensions.registry.install(
        manifest("acme.watch", host=["chat.approvals", "app"]), state=ExtensionState.READY
    )
    sink = Sink()
    bus = built.model.services.bus
    bus.subscribe(bus.attach("acme.watch", sink), list(wires))
    return sink


async def test_the_model_and_channel_are_built_over_the_extensions_runtime(
    tmp_path: Path,
) -> None:
    built, extensions = _build(tmp_path)

    assert isinstance(built.model, HostModel)
    assert isinstance(built.channel, HostChannel)
    assert built.channel._model is built.model
    assert built.channel._hooks is extensions.hooks
    assert built.channel._processes is extensions.processes
    sink = _listen(built, extensions, "self.state_changed")
    extensions.registry.set_state("acme.watch", ExtensionState.FAILED, "crashed")
    assert sink.values("self.state_changed")


async def test_it_listens_on_the_process_wide_approvals_and_busy_leases(tmp_path: Path) -> None:
    built, extensions = _build(tmp_path)
    sink = _listen(built, extensions, "chat.approvals.changed", "app.activity_changed")

    approval_events.announce(
        ApprovalChange(
            session_id="boot",
            requirement_id="q1",
            run_id="r",
            state=ApprovalState.WAITING,
            origin="main",
        )
    )
    ActivityRegistry.shared().hold(BusyKind.BACKGROUND, "test", "working")
    for _ in range(5):
        await asyncio.sleep(0)

    assert [v["requirement_id"] for v in sink.values("chat.approvals.changed")] == ["q1"]
    # The approval moved the activity once (idle), the lease again (running).
    assert [v["state"] for v in sink.values("app.activity_changed")][-1] == "running"


async def test_the_hook_gate_is_given_the_extensions_hooks(tmp_path: Path) -> None:
    built, extensions = _build(tmp_path)

    assert extension_hook_gate._answerer is extensions.hooks
    assert built.model is not None


async def test_the_workspace_poll_starts_after_the_installs_and_stops_with_the_runtime(
    tmp_path: Path,
) -> None:
    built, _ = _build(tmp_path)
    watcher = built._signals._watcher
    assert watcher._task is None

    await built.start_signals()
    assert watcher._task is not None and not watcher._task.done()

    await built.stop()
    assert watcher._task is None


async def test_stop_closes_the_channel_before_the_signals() -> None:
    order: list[str] = []

    class Stopper:
        def __init__(self, name: str) -> None:
            self.name = name

        async def stop(self) -> None:
            order.append(self.name)

    runtime = HostModelRuntime(
        model=SimpleNamespace(),  # type: ignore[arg-type]
        channel=Stopper("channel"),  # type: ignore[arg-type]
        signals=Stopper("signals"),  # type: ignore[arg-type]
    )

    await runtime.stop()
    await runtime.stop()

    assert order == ["channel", "signals", "channel", "signals"]


async def test_a_runtime_stopped_before_its_signals_started_stops_quietly(tmp_path: Path) -> None:
    built, _ = _build(tmp_path)

    await built.stop()
    await built.stop()
