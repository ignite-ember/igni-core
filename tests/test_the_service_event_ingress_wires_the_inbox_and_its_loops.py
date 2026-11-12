"""What the backend builds to take a service's events in: the inbox, the held posts, the pull.

``ServiceEventIngress.build`` is run over a real backend rig and a real
database. Its RPC answers ``deliver_service_events`` through the real
inbox, so an event reaches the extension's handler and an item reaches a
chat of the pool; the pull exists only where igni fetches its own events,
and start and stop reach the pull and the flusher in order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend import service_event_ingress
from igni_core.backend.pool_rpcs import ServiceEventRpc
from igni_core.backend.service_event_ingress import ServiceEventIngress
from igni_core.core.db.database import Database
from igni_core.core.db.urls import DatabaseUrl
from igni_core.extensions.host.wire import HookAnswer, Outcome
from igni_core.notifications.server_client import SignedInServer
from igni_core.protocol import messages as msg
from tests.host_model_helpers import HostRig
from tests.service_item_rig import item, message
from tests.storage_backends import Scopes


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def build(rig: HostRig, tmp_path: Path, *, fetches_own_events: bool) -> ServiceEventIngress:
    assert rig.orchestrator is not None
    return ServiceEventIngress.build(
        producer_db=Database(DatabaseUrl.for_file(tmp_path / "e.db"), scope=Scopes.fresh()),
        signed_in=SignedInServer(api_url=lambda: None, session=lambda: None),
        orchestrator=rig.orchestrator,
        hooks=rig.hooks,
        fetches_own_events=fetches_own_events,
    )


async def deliver(rig: HostRig, request_id: str, *events: Any) -> Any:
    assert rig.orchestrator is not None
    await rig.orchestrator.dispatch(
        msg.RPCRequest(
            id=request_id,
            method="deliver_service_events",
            args={"events": [e.model_dump(mode="json") for e in events]},
        )
    )
    [answer] = [m for m in rig.windows.of(msg.RPCResponse) if m.id == request_id]
    assert answer.error is None, answer.error
    return answer.result


async def test_the_rpc_hands_an_event_to_its_extension_and_lands_an_item_in_a_chat(
    rig: HostRig, tmp_path: Path
) -> None:
    ingress = build(rig, tmp_path, fetches_own_events=False)
    assert isinstance(ingress.rpc(), ServiceEventRpc)
    assert rig.orchestrator is not None
    rig.orchestrator.mount(ingress.rpc())
    ext = rig.install("acme.claims")
    asked: list[dict[str, Any]] = []

    def send(frame: str) -> None:
        sent = json.loads(frame)
        asked.append(sent)
        rig.hooks.answered(ext, HookAnswer(hook=sent["id"], outcome=Outcome.ALLOW))

    rig.hooks.connect(ext, send)

    handled = await deliver(rig, "d1", item("evt-1"))
    landed = await deliver(rig, "d2", message("msg-1", text="Claim arrived."))
    await rig.settle()

    assert handled["acknowledge"] == ["evt-1"] and handled["not_handled"] == []
    [frame] = asked
    assert frame["event"] == "ServiceEvent" and frame["value"]["event_id"] == "evt-1"
    assert landed["acknowledge"] == ["msg-1"]
    echoes = [m for m in rig.windows.of(msg.UserMessageReceived) if m.text == "Claim arrived."]
    assert [e.service_client_id for e in echoes] == ["svc-claims"], "posted by the desk"


async def test_an_event_nobody_handles_is_reported_not_handled(
    rig: HostRig, tmp_path: Path
) -> None:
    rig.install("acme.claims")
    assert rig.orchestrator is not None
    rig.orchestrator.mount(build(rig, tmp_path, fetches_own_events=False).rpc())

    result = await deliver(rig, "d1", item("evt-1"))

    assert [n["event_id"] for n in result["not_handled"]] == ["evt-1"]


class Loop:
    """Stands in for the pull and the flusher; records the order of calls."""

    calls: list[str] = []

    def __init__(self, name: str, fail_stop: bool = False) -> None:
        self.name = name
        self.fail_stop = fail_stop

    def start(self) -> None:
        self.calls.append(f"start {self.name}")

    async def stop(self) -> None:
        self.calls.append(f"stop {self.name}")
        if self.fail_stop:
            raise RuntimeError("stop failed")


def wired(pull: Loop | None, flusher: Loop) -> ServiceEventIngress:
    Loop.calls = []
    return ServiceEventIngress(
        inbox=None,  # type: ignore[arg-type]
        held_posts=flusher,  # type: ignore[arg-type]
        event_pull=pull,  # type: ignore[arg-type]
    )


async def test_start_and_stop_reach_the_pull_and_the_flusher_in_order() -> None:
    ingress = wired(Loop("pull"), Loop("flusher"))

    ingress.start()
    await ingress.stop()

    assert Loop.calls == ["start pull", "start flusher", "stop pull", "stop flusher"]


async def test_without_a_pull_only_the_flusher_runs() -> None:
    ingress = wired(None, Loop("flusher"))

    ingress.start()
    await ingress.stop()

    assert Loop.calls == ["start flusher", "stop flusher"]


async def test_a_pull_that_fails_to_stop_leaves_the_flusher_for_the_caller_to_see() -> None:
    ingress = wired(Loop("pull", fail_stop=True), Loop("flusher"))

    with pytest.raises(RuntimeError, match="stop failed"):
        await ingress.stop()


async def test_the_pull_exists_only_where_igni_fetches_its_own_events(
    rig: HostRig, tmp_path: Path
) -> None:
    desktop = build(rig, tmp_path, fetches_own_events=True)
    cloud = build(rig, tmp_path, fetches_own_events=False)

    assert isinstance(desktop._event_pull, service_event_ingress.PersonEventPull)
    assert cloud._event_pull is None
