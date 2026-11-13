"""``HostServices.build``: every collaborator the host model needs, composed once.

The defaults a config leaves out, the backend read each time rather than
captured, and the windows' listeners wired to the signals they produce.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from igni_core.backend.activity import Activity, ActivityState
from igni_core.core.decisions.laya import process_laya
from igni_core.core.session.client_state import ClientStateRead, ClientStateResult
from igni_core.extensions.host.model_config import HostModelConfig
from igni_core.extensions.host.services import HostServices
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.store import ExtensionStore
from tests.host_model_helpers import Sink, manifest


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {}

    def get_for_client(self, client_id: str) -> ClientStateRead:
        return ClientStateRead(ok=True, values=dict(self.values.get(client_id, {})))

    def set_value(self, client_id: str, key: str, value: str) -> ClientStateResult:
        self.values.setdefault(client_id, {})[key] = value
        return ClientStateResult(ok=True)

    def delete_value(self, client_id: str, key: str) -> ClientStateResult:
        self.values.get(client_id, {}).pop(key, None)
        return ClientStateResult(ok=True)


def idle() -> Activity:
    return Activity(state=ActivityState.IDLE, suspendable=True)


def running() -> Activity:
    return Activity(state=ActivityState.RUNNING, suspendable=False)


class Rig:
    def __init__(self, **config: Any) -> None:
        self.sent: list[Any] = []
        self.backend = SimpleNamespace(
            broadcast=self.sent.append, client_state=MemoryStore(), activity=idle
        )
        self.registry = ExtensionRegistry()
        self.services = HostServices.build(
            HostModelConfig(backend=self.backend, registry=self.registry, **config)
        )

    def listen(self, *wires: str, **permissions: Any) -> Sink:
        self.registry.install(manifest("acme.one", **permissions), state=ExtensionState.READY)
        sink = Sink()
        self.services.bus.subscribe(self.services.bus.attach("acme.one", sink), list(wires))
        return sink


def test_a_config_without_decisions_uses_the_process_wide_laya() -> None:
    assert Rig().services.decisions is process_laya


def test_a_config_with_decisions_keeps_them() -> None:
    mine = object()

    assert Rig(decisions=mine).services.decisions is mine


def test_a_config_without_a_store_gets_a_fresh_one_each_time() -> None:
    first, second = Rig().services.extensions.store, Rig().services.extensions.store

    assert isinstance(first, ExtensionStore) and isinstance(second, ExtensionStore)
    assert first is not second


def test_a_config_with_a_store_keeps_it() -> None:
    store = ExtensionStore()

    assert Rig(store=store).services.extensions.store is store


def test_a_window_model_change_is_pushed_to_the_windows() -> None:
    rig = Rig()
    rig.services.windows.registry.attached("w-1", connection_id="c-1")

    rig.services.windows.write("w-1", "route", [{"kind": "chat", "label": "Chat"}])

    pushed = [m for m in rig.sent if getattr(m, "channel", "") == "window_model_changed"]
    assert [m.payload["field"] for m in pushed] == ["route"]


def test_a_window_s_route_change_reaches_a_listener_as_a_signal() -> None:
    rig = Rig()
    heard = rig.listen("ui.navigated", host=["ui"])
    rig.services.windows.registry.attached("w-1", connection_id="c-1")

    rig.services.windows.write("w-1", "route", [{"kind": "chat", "label": "Chat"}])

    assert [v["window"] for v in heard.values("ui.navigated")] == ["w-1"]


def test_a_window_opening_reaches_a_listener_as_a_signal() -> None:
    rig = Rig()
    heard = rig.listen("windows.opened", host=["ui"])

    rig.services.windows.registry.attached("w-1", connection_id="c-1")

    assert [v["id"] for v in heard.values("windows.opened")] == ["w-1"]


def test_a_session_switched_in_a_window_is_told_through_the_observations() -> None:
    rig = Rig()
    heard = rig.listen("session.switched", host=["sessions"])
    rig.services.windows.registry.attached("w-1", connection_id="c-1")

    rig.services.windows.write("w-1", "session", "s-2")

    assert heard.values("session.switched") == [{"session_id": "s-2", "window": "w-1"}]


def test_the_backend_s_activity_is_read_each_time_not_captured() -> None:
    rig = Rig()
    heard = rig.listen("app.activity_changed", host=["app"])
    rig.backend.activity = running

    rig.services.activity_watch.moved()

    assert [v["state"] for v in heard.values("app.activity_changed")] == ["running"]
