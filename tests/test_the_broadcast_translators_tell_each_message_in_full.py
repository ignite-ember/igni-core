"""How each small broadcast translator tells what it was handed, in full.

The wire-to-signal translation as a whole is pinned through the real
transport in ``test_what_igni_sends_its_windows_reaches_extensions_as_signals.py``;
this file pins the pieces that file only touches by consequence: the
dispatch merge, the run header, the status memory's choices, the task
and scheduler payloads, the view's spec, the values read again and the
composition root.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.schemas_visualization import VisualizationActionDispatchedEvent
from igni_core.core.session.event_log_schema import VisualizationDeltaPayload
from igni_core.extensions.host.broadcasts.session_values import HostSessionValues
from igni_core.extensions.host.broadcasts.signal import RunRef, merged
from igni_core.extensions.host.broadcasts.status import Connection, StatusSignals
from igni_core.extensions.host.broadcasts.task_signals import SchedulerSignal, TaskSignals
from igni_core.extensions.host.broadcasts.translators import Translators
from igni_core.extensions.host.broadcasts.views import ViewAction, ViewShown
from igni_core.extensions.host.signal_values import Emitter
from igni_core.extensions.host.state.status import StatusMemory
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import HostRig


class Told:
    """What an emitter was asked to put on the bus: ``(wire, value)``."""

    def __init__(self) -> None:
        self.got: list[tuple[str, Any]] = []
        self.emitter = Emitter(self._emit)

    def _emit(self, wire: str, value: Any, **_: Any) -> None:
        self.got.append((wire, value))

    def wires(self) -> list[str]:
        return [w for w, _ in self.got]

    def values(self, wire: str) -> list[Any]:
        return [v for w, v in self.got if w == wire]


# ── signal.py ─────────────────────────────────────────────────────


def test_merging_no_tables_gives_an_empty_table():
    assert merged() == {}


def test_a_merge_keeps_every_handler_of_every_table():
    one, two, three = (lambda m: None), (lambda m: None), (lambda m: None)

    out = merged({"a": one}, {}, {"b": two, "c": three})

    assert out == {"a": one, "b": two, "c": three}


def test_a_class_claimed_by_two_translators_is_refused_by_name():
    with pytest.raises(RuntimeError, match=r"'b' is translated twice"):
        merged({"a": 1, "b": 2}, {"c": 3}, {"b": 4})


def test_a_run_header_names_the_session_the_run_and_its_parent():
    child = msg.RunStarted(session_id="s1", run_id="r2", parent_run_id="r1", agent_name="x")
    top = msg.RunStarted(session_id="s1", run_id="r1")

    assert RunRef.run_of(child).model_dump() == {
        "session_id": "s1",
        "run_id": "r2",
        "parent_run_id": "r1",
    }
    assert RunRef.run_of(top).parent_run_id == ""


# ── status.py ─────────────────────────────────────────────────────


def _status(session: str = "s1", **fields: Any) -> msg.StatusUpdate:
    return msg.StatusUpdate(session_id=session, **fields)


def _told_statuses(*updates: msg.StatusUpdate) -> Told:
    told = Told()
    signals = StatusSignals(told.emitter, StatusMemory())
    for update in updates:
        signals.handlers()[msg.StatusUpdate](update)
    return told


def test_status_claims_the_status_update_and_nothing_else():
    signals = StatusSignals(Told().emitter, StatusMemory())

    assert set(signals.handlers()) == {msg.StatusUpdate}


def test_the_connection_is_read_from_the_update():
    connection = Connection.of_status(_status(cloud_connected=True, model_configured=False))

    assert connection.model_dump() == {"cloud_connected": True, "model_configured": False}


def test_the_first_status_tells_the_status_the_connection_and_the_account():
    told = _told_statuses(_status(cloud_connected=True, cloud_email="a@x.test", cloud_name="Ann"))

    assert told.wires() == ["chat.status_changed", "app.connection_changed", "account.changed"]
    assert told.values("app.connection_changed") == [
        {"cloud_connected": True, "model_configured": True}
    ]
    assert told.values("account.changed") == [
        {"signed_in": True, "email": "a@x.test", "name": "Ann", "org": ""}
    ]


def test_an_unchanged_status_tells_only_the_status():
    same = {"cloud_connected": True, "cloud_email": "a@x.test"}

    told = _told_statuses(_status(**same), _status(**same))

    assert told.wires() == [
        "chat.status_changed",
        "app.connection_changed",
        "account.changed",
        "chat.status_changed",
    ]


def test_a_model_set_up_alone_tells_the_connection_and_not_the_account():
    told = _told_statuses(_status(model_configured=False), _status(model_configured=True))

    assert told.wires()[3:] == ["chat.status_changed", "app.connection_changed"]
    assert told.values("app.connection_changed")[-1]["model_configured"] is True


def test_a_new_email_alone_tells_the_account_and_not_the_connection():
    told = _told_statuses(_status(cloud_email="a@x.test"), _status(cloud_email="b@x.test"))

    assert told.wires()[3:] == ["chat.status_changed", "account.changed"]
    assert told.values("account.changed")[-1]["email"] == "b@x.test"


def test_each_session_is_told_afresh_on_its_first_status():
    told = _told_statuses(_status("s1"), _status("s2"))

    assert told.wires().count("app.connection_changed") == 2
    assert told.wires().count("account.changed") == 2


def test_a_connection_that_flips_back_is_told_each_time():
    told = _told_statuses(
        _status(cloud_connected=True), _status(cloud_connected=False), _status(cloud_connected=True)
    )

    assert [v["cloud_connected"] for v in told.values("app.connection_changed")] == [
        True,
        False,
        True,
    ]


# ── task_signals.py ───────────────────────────────────────────────


def _tasks() -> tuple[TaskSignals, Told]:
    told = Told()
    return TaskSignals(told.emitter), told


def _push(channel: str, session_id: str = "s1", **payload: Any) -> msg.PushNotification:
    return msg.PushNotification(channel=channel, session_id=session_id, payload=payload)


def test_tasks_claim_their_messages_and_the_three_push_channels():
    tasks, _ = _tasks()

    assert set(tasks.handlers()) == {
        msg.TaskCreated,
        msg.TaskUpdated,
        msg.TaskIteration,
        msg.TaskStateUpdated,
        msg.SchedulerEvent,
    }
    assert {c.value for c in tasks.push_handlers()} == {
        "scheduler_started",
        "scheduler_completed",
        "todos_updated",
    }


def test_a_created_task_is_told_with_its_title_assignee_and_status():
    tasks, told = _tasks()

    tasks.handlers()[msg.TaskCreated](
        msg.TaskCreated(session_id="s1", task_id="t1", title="Écrire 日本", assignee="me")
    )

    assert told.got == [
        (
            "tasks.created",
            {
                "session_id": "s1",
                "task_id": "t1",
                "title": "Écrire 日本",
                "assignee": "me",
                "status": told.values("tasks.created")[0]["status"],
            },
        )
    ]
    assert told.values("tasks.created")[0]["status"] != ""


def test_an_updated_task_carries_no_title():
    tasks, told = _tasks()

    tasks.handlers()[msg.TaskUpdated](msg.TaskUpdated(session_id="s1", task_id="t1", status="done"))

    (value,) = told.values("tasks.updated")
    assert value["title"] == ""
    assert (value["task_id"], value["status"], value["session_id"]) == ("t1", "done", "s1")


def test_the_schedulers_own_message_is_told_with_its_kind_and_what_it_left():
    tasks, told = _tasks()

    tasks.handlers()[msg.SchedulerEvent](
        msg.SchedulerEvent(event_type="fired", task_id="c1", description="nightly", result="ok")
    )

    assert told.got == [
        (
            "scheduler.event",
            {"kind": "fired", "task_id": "c1", "description": "nightly", "result": "ok"},
        )
    ]


def test_a_started_scheduled_task_is_told_without_a_result():
    tasks, told = _tasks()

    tasks.push_handlers()[_channel("scheduler_started")](
        _push("scheduler_started", task_id="c1", description="nightly")
    )

    assert told.values("scheduler.event") == [
        {"kind": "started", "task_id": "c1", "description": "nightly", "result": ""}
    ]


def test_a_completed_scheduled_task_is_told_with_its_result():
    tasks, told = _tasks()

    tasks.push_handlers()[_channel("scheduler_completed")](
        _push("scheduler_completed", task_id="c1", description="nightly", result="done ✓")
    )

    assert told.values("scheduler.event") == [
        {"kind": "completed", "task_id": "c1", "description": "nightly", "result": "done ✓"}
    ]


@pytest.mark.parametrize("channel", ["scheduler_started", "scheduler_completed"])
def test_a_scheduler_push_that_does_not_fit_is_dropped_and_said_so(channel, caplog):
    tasks, told = _tasks()

    with caplog.at_level(logging.WARNING):
        tasks.push_handlers()[_channel(channel)](_push(channel, description="no task id"))

    assert told.got == []
    assert channel in caplog.text


def test_the_todo_list_is_told_with_the_session_of_the_push():
    tasks, told = _tasks()
    item = {"content": "Write", "status": "pending"}

    tasks.push_handlers()[_channel("todos_updated")](
        _push("todos_updated", session_id="s9", todos=[item])
    )

    (value,) = told.values("tasks.todos_changed")
    assert value["session_id"] == "s9"
    assert [t["content"] for t in value["todos"]] == ["Write"]
    assert value["todos"][0]["activeForm"] == ""


def test_an_emptied_todo_list_is_told_empty():
    tasks, told = _tasks()

    tasks.push_handlers()[_channel("todos_updated")](_push("todos_updated", todos=[]))

    assert told.values("tasks.todos_changed") == [{"session_id": "s1", "todos": []}]


def test_a_cancelled_task_is_told_even_when_unknown_and_empty_of_text():
    tasks, told = _tasks()

    tasks.cancelled("never-seen")

    assert told.values("scheduler.event") == [
        {"kind": "cancelled", "task_id": "never-seen", "description": "", "result": ""}
    ]
    assert SchedulerSignal.cancelled("x").kind == "cancelled"


def _channel(name: str):
    from igni_core.protocol.schemas.enums import PushChannel

    return PushChannel(name)


# ── views.py ──────────────────────────────────────────────────────


def _delta(**fields: Any) -> VisualizationDeltaPayload:
    return VisualizationDeltaPayload(spec_id="v1", final=True, **fields)


def test_a_card_with_its_own_source_keeps_it():
    shown = ViewShown.shown("s1", _delta(spec_json="{}", source="acme.charts", title="T"))

    assert shown.model_dump() == {
        "view_id": "v1",
        "session_id": "s1",
        "title": "T",
        "spec": {},
        "source": "acme.charts",
    }


def test_a_card_naming_no_source_comes_from_the_visualizer():
    assert ViewShown.shown("s1", _delta(spec_json="{}", source="")).source == "visualizer"


@pytest.mark.parametrize(
    ("spec_json", "spec"),
    [
        ("", {}),
        ("{", {}),
        ("[1, 2]", [1, 2]),
        ("null", None),
        ("3", 3),
        ('{"title": "Café 日本"}', {"title": "Café 日本"}),
    ],
)
def test_a_spec_is_what_was_streamed_or_empty_when_it_was_not_json(spec_json, spec):
    assert ViewShown.shown("s1", _delta(spec_json=spec_json)).spec == spec


def test_a_large_spec_is_carried_whole():
    big = {"rows": list(range(5000))}

    import json

    assert ViewShown.shown("s1", _delta(spec_json=json.dumps(big))).spec == big


def test_a_dispatched_action_is_told_with_its_parameters():
    event = VisualizationActionDispatchedEvent(action="drill", params={"id": 3, "tags": ["a"]})

    assert ViewAction.dispatched("s1", event).model_dump() == {
        "session_id": "s1",
        "action": "drill",
        "params": {"id": 3, "tags": ["a"]},
    }


# ── session_values.py ─────────────────────────────────────────────


class FakeGateway:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def account(self, session_id: str) -> str:
        self.asked.append(session_id)
        return f"account of {session_id!r}"


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def rpc(self, method: Any, params: Any, *, session_id: str) -> Any:
        self.calls.append((method, params, session_id))
        return {"loop_of": session_id}

    def default_session(self) -> str:
        return "boot"


def _values(backend: Any = None, gateway: Any = None):
    display_asked: list[str] = []

    async def display(session_id: str) -> str:
        display_asked.append(session_id)
        return f"display of {session_id!r}"

    values = HostSessionValues(gateway or FakeGateway(), backend or FakeBackend(), display)
    return values, display_asked


async def test_the_account_is_what_the_gateway_reads_for_that_session():
    gateway = FakeGateway()
    values, _ = _values(gateway=gateway)

    assert await values.account("s7") == "account of 's7'"
    assert gateway.asked == ["s7"]


async def test_the_display_is_what_the_reader_reads_for_that_session():
    values, asked = _values()

    assert await values.display("s7") == "display of 's7'"
    assert await values.display("") == "display of ''"
    assert asked == ["s7", ""]


async def test_the_loop_is_the_backends_loop_status_for_that_session():
    backend = FakeBackend()
    values, _ = _values(backend=backend)

    assert await values.loop("s7") == {"loop_of": "s7"}
    assert backend.calls == [(RpcMethod.LOOP_STATUS, {}, "s7")]


def test_the_default_session_is_the_backends():
    values, _ = _values()

    assert values.default_session() == "boot"


async def test_a_backend_replaced_after_construction_is_the_one_asked():
    backend = FakeBackend()
    values, _ = _values(backend=backend)

    async def rpc(method: Any, params: Any, *, session_id: str) -> str:
        return "replaced"

    backend.rpc = rpc  # type: ignore[method-assign]
    backend.default_session = lambda: "other"  # type: ignore[method-assign]

    assert await values.loop("s1") == "replaced"
    assert values.default_session() == "other"


async def test_a_failing_read_is_raised_to_whoever_asked():
    class Failing(FakeBackend):
        async def rpc(self, method: Any, params: Any, *, session_id: str) -> Any:
            raise ConnectionError("backend gone")

    values, _ = _values(backend=Failing())

    with pytest.raises(ConnectionError, match="backend gone"):
        await values.loop("s1")


# ── translators.py ────────────────────────────────────────────────


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[HostRig]:
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def test_the_composition_merges_every_translators_table_with_the_push_one(rig):
    async def display(session_id: str) -> Any:
        raise AssertionError("not read here")

    built = Translators.of(rig.model.services, display=display)

    assert set(built.handlers) == set(rig.model.broadcasts.handled())
    assert msg.PushNotification in built.handlers
    assert built.handlers[msg.PushNotification] == built.push.push
    assert {msg.StatusUpdate, msg.TaskCreated, msg.RunStarted, msg.HITLRequest} <= set(
        built.handlers
    )


def test_the_display_reader_is_given_by_keyword_only(rig):
    async def display(session_id: str) -> Any:
        return None

    with pytest.raises(TypeError):
        Translators.of(rig.model.services, display)  # type: ignore[misc]


async def test_the_display_reader_given_is_the_one_an_output_style_change_reads(rig):
    from tests.host_model_helpers import Sink
    from tests.test_what_igni_sends_its_windows_reaches_extensions_as_signals import EVERYTHING

    asked: list[str] = []

    async def display(session_id: str) -> Any:
        asked.append(session_id)
        return {"output_style": "terse", "verbose": True}

    bus = rig.model.services.bus
    extension = rig.install("acme.all", host=EVERYTHING, workspace="read")
    sink = Sink()
    bus.subscribe(bus.attach(extension, sink), ["app.display_changed"])
    built = Translators.of(rig.model.services, display=display)

    built.push.push(
        msg.PushNotification(channel="output_style_changed", session_id="s4", payload={})
    )
    await rig.settle()

    assert asked == ["s4"]
    assert sink.values("app.display_changed") == [{"output_style": "terse", "verbose": True}]
