"""The host model wiring reaches only the hubs it is handed.

``HostModelWiring.build`` takes its process-wide hubs (approvals, busy
leases, the hook gate) as a :class:`HostModelHubs` and touches no
singleton. Built here over hubs of the test's own, with the rest of the
backend a stand-in, so a wiring that reached the singletons instead would
leave these silent. The window connections it watches are told apart too:
only a detach is the model's business.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.backend.host_model_wiring import HostModelHubs, HostModelWiring
from igni_core.backend.observed_transport import ObservedTransport
from igni_core.core.activity import ActivityRegistry
from igni_core.core.activity.registry import BusyKind
from igni_core.core.approvals.events import ApprovalChange, ApprovalEvents, ApprovalState
from igni_core.extensions.host.hooks import ExtensionHooks
from igni_core.extensions.host.model import HostModel
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.protocol import messages as msg
from tests.host_model_helpers import CapturingTransport, Sink, manifest


class _Windows(CapturingTransport):
    """The windows, keeping a list of who watches their connections."""

    def __init__(self) -> None:
        super().__init__()
        self.watchers: list[Callable[[str, str], None]] = []

    def watch_connections(self, watcher: Callable[[str, str], None]) -> None:
        self.watchers.append(watcher)


class _Orchestrator:
    """The part of the orchestrator the wiring touches."""

    def __init__(self) -> None:
        self.client_state = SimpleNamespace()
        self.mounted: list[Any] = []
        self.inbound: list[Callable[..., Any]] = []
        self.pool = SimpleNamespace(runtimes=[])

    def mount(self, rpc: Any) -> None:
        self.mounted.append(rpc)

    def watch_inbound(self, watcher: Callable[..., Any]) -> None:
        self.inbound.append(watcher)


class _Built:
    def __init__(self, tmp_path: Path, loop: bool = True) -> None:
        self.windows = _Windows()
        self.transport = ObservedTransport(self.windows)
        self.orchestrator = _Orchestrator()
        self.registry = ExtensionRegistry()
        self.hooks = ExtensionHooks(registry=self.registry)
        self.installed: list[Any] = []
        self.hubs = HostModelHubs(
            approvals=ApprovalEvents(),
            activities=ActivityRegistry(),
            install_hooks=self.installed.append,
        )
        self.model: HostModel = HostModelWiring.build(
            orchestrator=self.orchestrator,  # type: ignore[arg-type]
            transport=self.transport,
            registry=self.registry,
            hooks=self.hooks,
            project_dir=tmp_path,
            hubs=self.hubs,
        )

    def listen(self, *wires: str, host: list[str]) -> Sink:
        self.registry.install(manifest("acme.watch", host=host), state=ExtensionState.READY)
        sink = Sink()
        self.model.services.bus.subscribe(
            self.model.services.bus.attach("acme.watch", sink), list(wires)
        )
        return sink


def test_the_hubs_are_a_frozen_bundle() -> None:
    hubs = HostModelHubs(
        approvals=ApprovalEvents(), activities=ActivityRegistry(), install_hooks=lambda hooks: None
    )

    with pytest.raises(ValidationError):
        hubs.approvals = ApprovalEvents()  # type: ignore[misc]


async def test_igni_s_hook_events_are_installed_from_the_hubs_gate(tmp_path: Path) -> None:
    built = _Built(tmp_path)

    assert built.installed == [built.hooks]


async def test_the_host_rpcs_are_mounted_and_inbound_rpcs_watched(tmp_path: Path) -> None:
    built = _Built(tmp_path)

    assert len(built.orchestrator.mounted) == 1
    assert len(built.orchestrator.inbound) == 1


async def test_an_approval_announced_on_the_given_hub_reaches_the_model(tmp_path: Path) -> None:
    built = _Built(tmp_path)
    sink = built.listen("chat.approvals.changed", host=["chat.approvals"])

    built.hubs.approvals.announce(
        ApprovalChange(
            session_id="boot",
            requirement_id="q1",
            run_id="r",
            state=ApprovalState.WAITING,
            origin="main",
            tool_name="shell",
        )
    )

    assert [(v["requirement_id"], v["state"]) for v in sink.values("chat.approvals.changed")] == [
        ("q1", "waiting")
    ]


async def test_a_busy_lease_on_the_given_registry_moves_the_activity(tmp_path: Path) -> None:
    built = _Built(tmp_path)
    sink = built.listen("app.activity_changed", host=["app"])

    lease = built.hubs.activities.hold(BusyKind.BACKGROUND, "test", "working")
    for _ in range(5):
        await asyncio.sleep(0)

    assert [v["state"] for v in sink.values("app.activity_changed")] == ["running"]
    lease.release()


async def test_an_outbound_message_is_seen_by_the_model_before_the_windows(tmp_path: Path) -> None:
    built = _Built(tmp_path)
    sink = built.listen("chat.run_completed", host=["chat.read"])

    await built.transport.send(msg.RunCompleted(session_id="boot", run_id="r1", duration=2.0))

    assert [m.type for m in built.windows.sent] == ["run_completed"]
    assert sink.values("chat.run_completed")


async def test_only_a_detached_connection_is_the_models_business(tmp_path: Path) -> None:
    built = _Built(tmp_path)
    sink = built.listen("windows.opened", "windows.closed", host=["windows"])
    [watcher] = built.windows.watchers
    registry = built.model.services.windows.registry
    detached: list[str] = []
    original = registry.detached
    registry.detached = lambda connection_id: (
        detached.append(connection_id),
        original(connection_id),
    )[1]  # type: ignore[method-assign]

    watcher("attached", "ws-1")
    watcher("something-else", "ws-2")
    assert detached == []

    watcher("detached", "ws-1")
    assert detached == ["ws-1"]
    assert not sink.values("windows.closed")


def test_with_no_running_loop_the_wiring_still_builds_and_skips_the_activity_nudge(
    tmp_path: Path,
) -> None:
    built = _Built(tmp_path)

    built.hubs.activities.hold(BusyKind.BACKGROUND, "test", "working")

    assert built.model is not None


async def test_a_change_in_the_registry_reaches_the_extension_it_is_about(tmp_path: Path) -> None:
    built = _Built(tmp_path)
    sink = built.listen("self.state_changed", host=["chat.read"])

    built.registry.set_state("acme.watch", ExtensionState.FAILED, "crashed")

    [changed] = sink.values("self.state_changed")
    assert changed["state"] == ExtensionState.FAILED
