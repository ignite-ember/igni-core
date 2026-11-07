"""What igni sends its windows reaches an entitled extension as the model's signals.

:class:`~igni_core.extensions.host.broadcasts.BroadcastSignals` is the
translation from igni's private wire (run events, push channels, RPC
writes) to the host model's published signals. A field renamed on the
wire is meant to be fixed there, once — so each translation is pinned
here by sending the wire message through the backend's real, observed
transport (:class:`~tests.host_model_helpers.HostRig`) and reading what
an extension holding every grant was told.

The grant-filtering itself (who hears what) is
``test_an_extension_hears_only_what_it_was_granted.py``; this file is
about the shape of what is heard.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from igni_core.extensions.host import coverage
from igni_core.extensions.host.broadcasts import RpcWrite, ViewShown
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.signal_values import Signal
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.be_events import TaskSnapshot
from tests.host_model_helpers import HostRig, Sink

EVERYTHING = [p.name for p in HOST_SCHEMA.permissions if p.name != "workspace"]
ALL_SIGNALS = [HOST_SCHEMA.wire_name(o, s) for o in HOST_SCHEMA.objects for s in o.signals]


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[HostRig]:
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


@pytest.fixture
def heard(rig: HostRig) -> Sink:
    """An extension granted everything, the workspace included, listening to every signal."""
    assert rig.model is not None
    extension = rig.install("acme.all", host=EVERYTHING, workspace="read")
    sink = Sink()
    rig.model.services.bus.subscribe(rig.model.services.bus.attach(extension, sink), ALL_SIGNALS)
    return sink


async def _send(rig: HostRig, *messages: msg.Message) -> None:
    for message in messages:
        await rig.transport.send(message)
    await rig.settle()


def _push(channel: str, session_id: str = "boot", **payload: Any) -> msg.PushNotification:
    return msg.PushNotification(channel=channel, session_id=session_id, payload=payload)


# ── Run events ────────────────────────────────────────────────────


async def test_a_run_is_told_from_start_to_finish_with_the_files_it_changed(rig, heard):
    (rig.project / "src").mkdir()
    await _send(
        rig,
        msg.RunStarted(session_id="boot", run_id="r1", agent_name="main", model="small"),
        msg.ReasoningStarted(session_id="boot", run_id="r1"),
        msg.ContentDelta(session_id="boot", text="Looking.", is_thinking=True),
        _push("file_edited", path=str(rig.project / "src" / "app.py")),
        _push("file_edited", path="src/util.py"),
        _push("file_edited", path="/etc/passwd"),
        _push("file_edited", path=""),
        msg.ToolStarted(
            session_id="boot", run_id="r1", tool_name="save_file", friendly_name="Write"
        ),
        msg.ToolCompleted(session_id="boot", run_id="r1", summary="wrote", is_error=False),
        msg.ToolError(session_id="boot", run_id="r1", error="disk full"),
        msg.ModelCompleted(session_id="boot", run_id="r1", input_tokens=5, output_tokens=7),
        msg.StreamingDone(session_id="boot", run_id="r1"),
        msg.RunCompleted(
            session_id="boot", run_id="r1", input_tokens=5, output_tokens=7, duration=1.5
        ),
    )

    assert heard.values("chat.run_started") == [
        {
            "session_id": "boot",
            "run_id": "r1",
            "parent_run_id": "",
            "agent_name": "main",
            "model": "small",
        }
    ]
    assert heard.values("chat.reasoning_started")[0]["run_id"] == "r1"
    assert heard.values("chat.content_delta") == [
        {"session_id": "boot", "text": "Looking.", "thinking": True}
    ]
    # Inside the project, absolute or relative; outside it, never.
    assert [v["path"] for v in heard.values("chat.file_edited")] == ["src/app.py", "src/util.py"]
    assert heard.values("chat.tool_started")[0]["friendly_name"] == "Write"
    assert heard.values("chat.tool_completed")[0]["summary"] == "wrote"
    assert heard.values("chat.tool_error")[0]["error"] == "disk full"
    assert heard.values("chat.model_completed")[0]["output_tokens"] == 7
    assert heard.values("chat.streaming_done")[0]["run_id"] == "r1"
    (done,) = heard.values("chat.run_completed")
    assert done["changed_files"] == ["src/app.py", "src/util.py"]
    assert (done["input_tokens"], done["duration"]) == (5, 1.5)


async def test_a_sub_agents_run_does_not_reset_the_parents_files(rig, heard):
    await _send(
        rig,
        msg.RunStarted(session_id="boot", run_id="r1"),
        _push("file_edited", path="a.py"),
        msg.RunStarted(session_id="boot", run_id="r2", parent_run_id="r1"),
        msg.RunCompleted(session_id="boot", run_id="r2", parent_run_id="r1"),
        msg.RunCompleted(session_id="boot", run_id="r1"),
    )

    child, parent = heard.values("chat.run_completed")
    assert child["changed_files"] == []
    assert parent["changed_files"] == ["a.py"]


async def test_a_failed_run_drops_what_it_was_waiting_on(rig, heard):
    await _send(
        rig,
        msg.HITLRequest(session_id="boot", requirement_id="q1", tool_name="run_shell_command"),
        msg.RunError(session_id="boot", error="model went away"),
    )

    assert heard.values("chat.run_error") == [{"session_id": "boot", "error": "model went away"}]
    assert not rig.model.services.state.approvals.knows("boot")


async def test_messages_typing_info_and_errors_are_told(rig, heard):
    await rig.window("w-main", connection="ws-1")
    await _send(
        rig,
        msg.UserMessageReceived(session_id="boot", text="hi", client_id="ws-1", queued=True),
        msg.Typing(session_id="boot", text="draft", client_id="ws-1"),
        msg.Typing(session_id="boot", text="elsewhere", client_id="ws-unknown"),
        msg.Info(session_id="boot", text="fyi"),
        msg.Error(session_id="boot", text="oops"),
    )

    assert heard.values("chat.message_added") == [
        {
            "session_id": "boot",
            "text": "hi",
            "queued": True,
            "from_extension": None,
            "from_service": None,
        }
    ]
    assert [(v["text"], v["window"]) for v in heard.values("chat.typing")] == [
        ("draft", "w-main"),
        ("elsewhere", None),
    ]
    assert heard.values("chat.info") == [{"session_id": "boot", "text": "fyi"}]
    assert heard.values("chat.error") == [{"session_id": "boot", "text": "oops"}]


# ── Approvals ─────────────────────────────────────────────────────


async def test_an_approval_is_requested_detailed_and_resolved(rig, heard):
    request = msg.HITLRequest(
        requirement_id="q1",
        tool_name="run_shell_command",
        friendly_name="Bash",
        tool_args={"command": "ls"},
    )
    await _send(
        rig,
        msg.RunPaused(session_id="boot", run_id="r1", requirements=[request]),
        msg.RequirementDetails(session_id="boot", requirement_id="q1", details="lists files"),
        msg.RequirementDetails(session_id="boot", requirement_id="gone", details="ignored"),
    )

    (paused,) = heard.values("chat.run_paused")
    assert paused["requirements"][0]["tool_args"] == {"command": "ls"}
    assert heard.values("chat.approvals.requested")[0]["friendly_name"] == "Bash"
    (waiting,) = rig.model.services.state.approvals.waiting("boot")
    assert waiting.details == "lists files"
    assert len(heard.values("chat.approvals.details")) == 2

    await _send(rig, msg.RequirementResolved(session_id="boot", requirement_id="q1"))

    assert heard.values("chat.approvals.resolved") == [
        {"session_id": "boot", "requirement_id": "q1"}
    ]
    assert rig.model.services.state.approvals.knows("boot")
    assert rig.model.services.state.approvals.waiting("boot") == []


async def test_approvals_waiting_after_a_restart_are_taken_back_quietly(rig, heard):
    waiting = [
        msg.RunPaused(requirements=[msg.HITLRequest(requirement_id="q1", tool_name="x")]),
        msg.Info(text="not an approval"),
    ]

    rig.model.services.state.approvals.restore("boot", waiting)
    rig.model.services.state.approvals.restore("boot", waiting)

    assert [a.requirement_id for a in rig.model.services.state.approvals.waiting("boot")] == ["q1"]
    assert heard.values("chat.approvals.requested") == []


# ── Status ────────────────────────────────────────────────────────


async def test_the_connection_and_account_are_told_only_when_they_change(rig, heard):
    first = msg.StatusUpdate(session_id="boot", cloud_connected=True, cloud_email="a@x.test")
    same = msg.StatusUpdate(session_id="boot", cloud_connected=True, cloud_email="a@x.test")
    moved = msg.StatusUpdate(session_id="boot", cloud_connected=False, cloud_email="b@x.test")

    await _send(rig, first, same, moved)

    assert len(heard.values("chat.status_changed")) == 3
    assert [v["cloud_connected"] for v in heard.values("app.connection_changed")] == [True, False]
    assert len(heard.values("account.changed")) == 2


# ── Tasks, sessions, scheduler ────────────────────────────────────


async def test_tasks_sessions_and_the_scheduler_are_told(rig, heard):
    await _send(
        rig,
        msg.TaskCreated(session_id="boot", task_id="t1", title="Write", assignee="me"),
        msg.TaskUpdated(session_id="boot", task_id="t1", status="done"),
        msg.TaskIteration(session_id="boot", iteration=2, max_iterations=5),
        msg.TaskStateUpdated(session_id="boot", tasks=[TaskSnapshot(task_id="t1")]),
        msg.SessionListResult(sessions=[msg.SessionListEntry(session_id="s1", name="One")]),
        msg.SessionCleared(session_id="boot", new_session_id="s2", summary="fresh"),
        msg.SchedulerEvent(event_type="fired", task_id="cron1", description="nightly"),
    )

    assert heard.values("tasks.created")[0]["title"] == "Write"
    assert heard.values("tasks.updated")[0]["status"] == "done"
    assert heard.values("tasks.iteration") == [
        {"session_id": "boot", "iteration": 2, "max_iterations": 5}
    ]
    assert heard.values("tasks.snapshot")[0]["tasks"][0]["task_id"] == "t1"
    assert heard.values("sessions.listed")[0]["sessions"][0]["session_id"] == "s1"
    assert heard.values("sessions.cleared")[0]["new_session_id"] == "s2"
    assert heard.values("scheduler.event")[0]["kind"] == "fired"


# ── Push channels ─────────────────────────────────────────────────


async def test_scheduler_pushes_become_scheduler_events(rig, heard):
    await _send(
        rig,
        _push("scheduler_started", task_id="c1", description="nightly"),
        _push("scheduler_completed", task_id="c1", error="timed out"),
        _push("scheduler_event", task_id="c1", event_type="skipped"),
        _push("scheduler_event", task_id="c1"),
    )

    assert [(v["kind"], v["result"]) for v in heard.values("scheduler.event")] == [
        ("started", ""),
        ("completed", "timed out"),
        ("skipped", ""),
        ("event", ""),
    ]


async def test_an_agents_progress_and_a_finished_view_are_told(rig, heard):
    await _send(
        rig,
        _push("orchestrate_progress", step=1),
        _push("orchestrate_event", type="agent_started", name="reviewer"),
        _push("orchestrate_event"),
        _push("orchestrate_event", type="visualization_delta", spec_id="v1", json="{"),
        _push(
            "orchestrate_event",
            type="visualization_delta",
            final=True,
            spec_id="v1",
            json='{"type": "Card"}',
            title="Spend",
        ),
    )

    assert [v["kind"] for v in heard.values("chat.agent_event")] == [
        "progress",
        "agent_started",
        "event",
    ]
    assert heard.values("chat.views.shown") == [
        {
            "view_id": "v1",
            "session_id": "boot",
            "title": "Spend",
            "spec": {"type": "Card"},
            "source": "visualizer",
        }
    ]


def test_a_view_whose_spec_is_not_json_is_shown_empty():
    from_wire = {"spec_id": "v1", "json": "{not json"}

    assert ViewShown.from_payload("s", from_wire).spec == {}


async def test_session_mode_plan_and_todo_pushes_are_told(rig, heard):
    await _send(
        rig,
        _push("session_named", name="Refactor"),
        _push("permission_mode_changed", mode="plan", previous="default"),
        _push("plan_submitted", run_id="r1"),
        _push("plan_decided", run_id="r1", decision="approved"),
        _push("todos_updated", todos=[{"content": "a"}]),
        _push("todos_updated", todos="not a list"),
        _push("http_retry_attempt", attempt=1),
        _push("http_retry_succeeded", attempt=2),
        _push("visualization_action_dispatched", action="refresh"),
    )

    assert heard.values("sessions.named") == [{"session_id": "boot", "name": "Refactor"}]
    assert heard.values("permissions.mode_changed") == [{"mode": "plan", "previous": "default"}]
    assert [v["kind"] for v in heard.values("plan.changed")] == ["submitted", "decided"]
    assert [v["todos"] for v in heard.values("tasks.todos_changed")] == [[{"content": "a"}], []]
    assert [v["kind"] for v in heard.values("chat.retry")] == ["attempt", "succeeded"]
    assert heard.values("chat.views.action") == [
        {"session_id": "boot", "action": "refresh", "params": {}}
    ]


async def test_process_and_workflow_pushes_are_told(rig, heard):
    await _send(
        rig,
        _push("process_started", pid=1),
        _push("process_line", line="ok"),
        _push("process_exited", code=0),
        _push("background_process_done", pid=1),
        _push("workflow_event", type="step", name="release"),
        _push("workflow_event"),
        _push("a_channel_nobody_translates", x=1),
    )

    assert [v["kind"] for v in heard.values("processes.changed")] == [
        "started",
        "line",
        "exited",
        "done",
    ]
    assert [(v["what"], v["name"]) for v in heard.values("workflows.changed")] == [
        ("step", "release"),
        ("event", None),
    ]


async def test_a_sign_in_and_an_output_style_are_read_again_and_told(rig, heard):
    await _send(rig, _push("login_status"), _push("login_result"), _push("output_style_changed"))

    assert len(heard.values("account.changed")) == 2
    assert len(heard.values("app.display_changed")) == 1


async def test_nothing_is_read_again_when_nobody_listens(rig):
    await _send(rig, _push("login_status"), _push("output_style_changed"))

    assert rig.model.services.work.idle()


# ── Writes made through RPC ───────────────────────────────────────


async def test_each_rpc_write_is_told_as_what_it_changed(rig, heard):
    after = rig.model.broadcasts.after_rpc
    writes: list[tuple[RpcMethod, dict[str, Any]]] = [
        (RpcMethod.SWITCH_MODEL, {"model_name": "large"}),
        (RpcMethod.MCP_CONNECT, {"server_name": "notes"}),
        (RpcMethod.SET_MCP_TOOL_ENABLED, {"server_name": "notes"}),
        (RpcMethod.INSTALL_PLUGIN, {"url": "https://git.example/p.git"}),
        (RpcMethod.SET_PLUGIN_ENABLED, {"name": "lint"}),
        (RpcMethod.PROMOTE_EPHEMERAL_AGENT, {"name": "helper"}),
        (RpcMethod.DISCARD_EPHEMERAL_AGENT, {"name": "helper"}),
        (RpcMethod.RELOAD_HOOKS, {}),
        (RpcMethod.CANCEL_SCHEDULED_TASK, {"task_id": "c1"}),
        (RpcMethod.TRUNCATE_HISTORY, {"run_id": "r3"}),
        (RpcMethod.DISCARD_INTERRUPTED_RUN, {"message_id": "m4"}),
        (RpcMethod.LIST_SESSIONS, {}),
    ]
    for method, args in writes:
        after(RpcWrite(method=method, args=args, session_id="boot"))
    await rig.settle()

    assert heard.values("models.changed") == [{"current": "large"}]
    assert [(v["what"], v["name"]) for v in heard.values("mcp.changed")] == [
        ("connected", "notes"),
        ("tool_toggled", "notes"),
    ]
    assert [(v["what"], v["name"]) for v in heard.values("plugins.changed")] == [
        ("installed", "https://git.example/p.git"),
        ("toggled", "lint"),
    ]
    assert [v["what"] for v in heard.values("agents.changed")] == ["promoted", "discarded"]
    assert heard.values("hooks.changed") == [{"what": "reloaded", "name": None, "payload": None}]
    assert heard.values("scheduler.event")[0]["kind"] == "cancelled"
    assert [(v["what"], v["run_id"]) for v in heard.values("chat.history_changed")] == [
        ("truncated", "r3"),
        ("discarded", "m4"),
    ]


async def test_writes_that_need_a_fresh_read_are_read_and_told(rig, heard):
    after = rig.model.broadcasts.after_rpc

    after(RpcWrite(method=RpcMethod.TOGGLE_VERBOSE, args={}, session_id="boot"))
    after(RpcWrite(method=RpcMethod.LOOP_PAUSE, args={}, session_id="boot"))
    after(RpcWrite(method=RpcMethod.CLEAR_CLOUD_CREDENTIALS, args={}, session_id="boot"))
    await rig.settle()

    assert len(heard.values("app.display_changed")) == 1
    assert len(heard.values("loop.changed")) == 1
    assert len(heard.values("account.changed")) == 1


async def test_an_extensions_own_settings_write_is_told_to_it_alone(rig, heard):
    other = Sink()
    rig.model.services.bus.subscribe(
        rig.model.services.bus.attach(rig.install("acme.other", host=EVERYTHING), other),
        ["self.settings_changed"],
    )
    rig.store.save_settings(rig.registry.get("acme.all").manifest, {"serviceUrl": "https://s"})

    for extension_id in ("acme.all", "acme.absent"):
        rig.model.broadcasts.after_rpc(
            RpcWrite(
                method=RpcMethod.SET_EXTENSION_SETTINGS,
                args={"extension_id": extension_id},
                session_id="boot",
            )
        )

    assert heard.values("self.settings_changed") == [{"values": {"serviceUrl": "https://s"}}]
    assert other.got == []


# ── Activity ──────────────────────────────────────────────────────


async def test_a_failing_activity_listener_does_not_stop_the_others(rig, heard):
    told: list[Any] = []

    def broken(_activity: Any) -> None:
        raise RuntimeError("listener bug")

    rig.model.services.activity_watch.listen(broken)
    rig.model.services.activity_watch.listen(told.append)
    rig.session.processing = True

    await _send(rig, msg.RunStarted(session_id="boot", run_id="r1"))

    assert told == heard.values("app.activity_changed")
    assert len(told) == 1


async def test_an_activity_that_cannot_be_read_is_not_told(rig, heard, monkeypatch):
    def unavailable() -> Any:
        raise RuntimeError("backend gone")

    monkeypatch.setattr(rig.model.services.backend, "activity", unavailable)

    await _send(rig, msg.RunStarted(session_id="boot", run_id="r1"))

    assert heard.values("app.activity_changed") == []


# ── The translation's own coverage ────────────────────────────────

#: The message types translated before the handlers became a typed table
#: (``_on_<type>`` methods); a class added or lost is a decision to make here.
TRANSLATED = {
    "user_message_received",
    "typing",
    "content_delta",
    "reasoning_started",
    "run_started",
    "run_completed",
    "run_error",
    "run_paused",
    "hitl_request",
    "requirement_resolved",
    "requirement_details",
    "streaming_done",
    "status_update",
    "info",
    "error",
    "tool_started",
    "tool_completed",
    "tool_error",
    "model_completed",
    "task_created",
    "task_updated",
    "task_iteration",
    "task_state_updated",
    "session_list_result",
    "session_cleared",
    "scheduler_event",
    "push_notification",
}


def _wire_type(kind: type[msg.Message]) -> str:
    return str(kind.model_fields["type"].default)


def test_each_message_translated_is_one_igni_broadcasts(rig):
    handled = rig.model.broadcasts.handled()

    assert {_wire_type(kind) for kind in handled} == TRANSLATED
    assert len({_wire_type(kind) for kind in handled}) == len(handled)
    assert {_wire_type(kind) for kind in handled} - {"push_notification"} <= set(
        coverage.BROADCASTS
    )


def test_every_push_channel_with_a_signal_is_translated(rig):
    assert rig.model.broadcasts.push_channels() == set(coverage.PUSH)


def _signals(base: type[Signal]) -> list[type[Signal]]:
    found: list[type[Signal]] = []
    for sub in base.__subclasses__():
        if "WIRE" in sub.__dict__:
            found.append(sub)
        found.extend(_signals(sub))
    return found


def test_each_signal_model_is_the_schemas_type_for_its_wire():
    types = {HOST_SCHEMA.wire_name(o, s): s.type for o in HOST_SCHEMA.objects for s in o.signals}
    signals = _signals(Signal)

    assert signals, "no signal models found"
    for model in signals:
        expression = types[model.WIRE]
        spec = HOST_SCHEMA.type(expression)
        assert spec is not None, f"{model.__name__}: {expression} is not a named type"
        assert set(model.model_fields) == {f.name for f in spec.fields}, model.__name__


async def test_what_is_heard_fits_the_schema(rig, heard):
    """Every value told in a run's life validates as its signal's schema type."""
    (rig.project / "src").mkdir()
    await _send(
        rig,
        msg.RunStarted(session_id="boot", run_id="r1"),
        msg.ContentDelta(session_id="boot", text="x"),
        _push("file_edited", path="src/a.py"),
        msg.HITLRequest(session_id="boot", requirement_id="q1", tool_name="x"),
        msg.TaskStateUpdated(session_id="boot", tasks=[TaskSnapshot(task_id="t1")]),
        _push("orchestrate_event", type="step"),
        _push("scheduler_event", task_id=None),
        msg.RunCompleted(session_id="boot", run_id="r1"),
    )
    types = {HOST_SCHEMA.wire_name(o, s): s.type for o in HOST_SCHEMA.objects for s in o.signals}

    assert heard.got
    for wire, value in heard.got:
        assert rig.model.services.types.check(types[wire], value) is None, (wire, value)
