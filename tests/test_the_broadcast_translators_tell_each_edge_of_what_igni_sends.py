"""The edges of the broadcast translators: who is narrowed, what is dropped, what falls back.

``test_what_igni_sends_its_windows_reaches_extensions_as_signals.py``
pins the shape of each translation on the common path. This file pins
the rest of the contract of :mod:`igni_core.extensions.host.broadcasts`:
a listener's workspace grant narrowing what it is told, a payload that
fits no model being dropped with a warning that says why, a failing
translator never reaching the sender, and the fallbacks (a session that
names none, a subject a write does not carry).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Coroutine
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from igni_core.core.approvals.events import ApprovalChange, ApprovalState
from igni_core.extensions.host.broadcasts.deferred_reads import DeferredReads
from igni_core.extensions.host.broadcasts.push_reads import read_push, read_push_from, read_union
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.rpc_write import RpcWrite
from igni_core.extensions.host.signal_values import Emitter
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import HostRig, Sink

ALL_SIGNALS = [HOST_SCHEMA.wire_name(o, s) for o in HOST_SCHEMA.objects for s in o.signals]
EVERYTHING = [p.name for p in HOST_SCHEMA.permissions if p.name != "workspace"]


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[HostRig]:
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def _listen(rig: HostRig, extension_id: str, wires: list[str], **permissions: Any) -> Sink:
    assert rig.model is not None
    permissions.setdefault("host", EVERYTHING)
    extension = rig.install(extension_id, **permissions)
    sink = Sink()
    bus = rig.model.services.bus
    wanted = [w for w in ALL_SIGNALS if any(fnmatch(w, pattern) for pattern in wires)]
    bus.subscribe(bus.attach(extension, sink), wanted)
    return sink


async def _send(rig: HostRig, *messages: msg.Message) -> None:
    for message in messages:
        await rig.transport.send(message)
    await rig.settle()


def _push(channel: str, session_id: str = "boot", **payload: Any) -> msg.PushNotification:
    return msg.PushNotification(channel=channel, session_id=session_id, payload=payload)


# ── A listener is told only what its workspace grant permits ──────


async def test_a_listener_is_told_of_edits_and_of_a_runs_files_only_within_its_workspace_paths(rig):
    (rig.project / "src").mkdir()
    (rig.project / "docs").mkdir()
    wires = ["chat.file_edited", "chat.run_completed"]
    docs = _listen(rig, "acme.docs", wires, workspace="read", workspacePaths=["docs/*"])
    everything = _listen(rig, "acme.every", wires, workspace="read")
    blind = _listen(rig, "acme.blind", wires)

    await _send(
        rig,
        msg.RunStarted(session_id="boot", run_id="r1"),
        _push("file_edited", path="src/zeta.py"),
        _push("file_edited", path="docs/b.md"),
        _push("file_edited", path=str(rig.project / "docs" / "a.md")),
        _push("file_edited", path="docs/ünï.md"),
        msg.RunCompleted(session_id="boot", run_id="r1", parent_run_id=""),
    )

    assert [v["path"] for v in docs.values("chat.file_edited")] == [
        "docs/b.md",
        "docs/a.md",
        "docs/ünï.md",
    ]
    assert [v["path"] for v in everything.values("chat.file_edited")] == [
        "src/zeta.py",
        "docs/b.md",
        "docs/a.md",
        "docs/ünï.md",
    ]
    assert docs.values("chat.run_completed")[0]["changed_files"] == [
        "docs/a.md",
        "docs/b.md",
        "docs/ünï.md",
    ]
    # Sorted, whatever order the edits came in.
    assert everything.values("chat.run_completed")[0]["changed_files"] == [
        "docs/a.md",
        "docs/b.md",
        "docs/ünï.md",
        "src/zeta.py",
    ]
    assert blind.values("chat.file_edited") == []
    assert blind.values("chat.run_completed")[0]["changed_files"] == []


async def test_an_edit_that_climbs_out_of_the_project_is_neither_told_nor_kept(rig):
    heard = _listen(
        rig,
        "acme.all",
        ["chat.file_edited", "chat.run_completed"],
        workspace="read",
    )

    await _send(
        rig,
        msg.RunStarted(session_id="boot", run_id="r1"),
        _push("file_edited", path="../outside.txt"),
        _push("file_edited", path="src/../../outside.txt"),
        _push("file_edited", path="src/../inside.txt"),
        msg.RunCompleted(session_id="boot", run_id="r1"),
    )

    assert [v["path"] for v in heard.values("chat.file_edited")] == ["inside.txt"]
    assert heard.values("chat.run_completed")[0]["changed_files"] == ["inside.txt"]


async def test_a_file_edit_that_is_not_a_path_is_dropped_and_logged(rig, caplog):
    heard = _listen(rig, "acme.all", ["chat.file_edited"], workspace="read")

    with caplog.at_level(logging.WARNING):
        await _send(rig, _push("file_edited"), _push("file_edited", path=7))

    assert heard.got == []
    assert "the file_edited push does not fit FileEditedPayload" in caplog.text


# ── A run's own fields ────────────────────────────────────────────


async def test_a_completed_run_carries_its_tokens_parent_and_duration(rig):
    heard = _listen(rig, "acme.all", ["chat.run_completed"], workspace="read")

    await _send(
        rig,
        msg.RunCompleted(
            session_id="boot",
            run_id="r2",
            parent_run_id="r1",
            input_tokens=3,
            output_tokens=4,
            reasoning_tokens=9,
            duration=2,
        ),
    )

    assert heard.values("chat.run_completed") == [
        {
            "session_id": "boot",
            "run_id": "r2",
            "parent_run_id": "r1",
            "input_tokens": 3,
            "output_tokens": 4,
            "reasoning_tokens": 9,
            "duration": 2.0,
            "changed_files": [],
        }
    ]


async def test_a_run_that_fails_without_having_started_is_still_told(rig):
    heard = _listen(rig, "acme.all", ["chat.run_error"])

    await _send(rig, msg.RunError(session_id="never-started", error="boom"))

    assert heard.values("chat.run_error") == [{"session_id": "never-started", "error": "boom"}]


async def test_the_files_of_a_failed_run_are_not_the_next_runs(rig):
    heard = _listen(rig, "acme.all", ["chat.run_completed"], workspace="read")

    await _send(
        rig,
        msg.RunStarted(session_id="boot", run_id="r1"),
        _push("file_edited", path="old.py"),
        msg.RunError(session_id="boot", error="boom"),
        msg.RunStarted(session_id="boot", run_id="r2"),
        msg.RunCompleted(session_id="boot", run_id="r2"),
    )

    assert heard.values("chat.run_completed")[0]["changed_files"] == []


# ── The conversation's values ─────────────────────────────────────


async def test_each_conversation_message_is_told_with_its_own_values(rig):
    heard = _listen(rig, "acme.all", ["chat.*", "sessions.*"])

    await _send(
        rig,
        msg.UserMessageReceived(
            session_id="boot",
            text="héllo ☃",
            client_id="ws-x",
            extension_id="acme.talker",
            service_client_id="svc-1",
        ),
        msg.ContentDelta(session_id="boot", text="plain", is_thinking=False),
        msg.ReasoningStarted(session_id="boot", run_id="r1"),
        msg.ToolStarted(
            session_id="boot",
            run_id="r1",
            tool_name="save_file",
            friendly_name="Write",
            args_summary="a.py",
        ),
        msg.ToolCompleted(
            session_id="boot",
            run_id="r1",
            summary="denied",
            is_error=True,
            is_rejected=True,
        ),
        msg.ModelCompleted(session_id="boot", run_id="r1", input_tokens=11, output_tokens=12),
        msg.SessionListResult(sessions=[]),
        msg.SessionCleared(session_id="boot", new_session_id="s2", summary="fresh start"),
        msg.Info(session_id="boot", text=""),
    )

    (added,) = heard.values("chat.message_added")
    assert (added["text"], added["from_extension"], added["from_service"]) == (
        "héllo ☃",
        "acme.talker",
        "svc-1",
    )
    assert heard.values("chat.content_delta") == [
        {"session_id": "boot", "text": "plain", "thinking": False}
    ]
    assert heard.values("chat.reasoning_started")[0]["run_id"] == "r1"
    started = heard.values("chat.tool_started")[0]
    assert (started["tool_name"], started["friendly_name"], started["args_summary"]) == (
        "save_file",
        "Write",
        "a.py",
    )
    completed = heard.values("chat.tool_completed")[0]
    assert (completed["summary"], completed["is_error"], completed["is_rejected"]) == (
        "denied",
        True,
        True,
    )
    model = heard.values("chat.model_completed")[0]
    assert (model["input_tokens"], model["output_tokens"]) == (11, 12)
    assert heard.values("sessions.listed") == [{"sessions": []}]
    assert heard.values("sessions.cleared") == [
        {"session_id": "boot", "new_session_id": "s2", "summary": "fresh start"}
    ]
    assert heard.values("chat.info") == [{"session_id": "boot", "text": ""}]


async def test_a_message_or_typing_makes_the_window_it_came_from_the_active_one(rig):
    registry = rig.model.services.windows.registry
    await rig.window("w-one", connection="ws-1")
    await rig.window("w-two", connection="ws-2")
    registry.touch("w-two")
    assert registry.active() == "w-two"

    await _send(rig, msg.UserMessageReceived(session_id="boot", text="hi", client_id="ws-1"))
    assert registry.active() == "w-one"

    await _send(rig, msg.Typing(session_id="boot", text="d", client_id="ws-2"))
    assert registry.active() == "w-two"

    # A connection that belongs to no window moves nothing.
    await _send(rig, msg.Typing(session_id="boot", text="d", client_id="ws-nobody"))
    await _send(rig, msg.UserMessageReceived(session_id="boot", text="hi", client_id="ws-nobody"))
    assert registry.active() == "w-two"


# ── Approvals ─────────────────────────────────────────────────────


async def test_a_pause_with_nothing_to_approve_is_told_with_no_approvals(rig):
    heard = _listen(rig, "acme.all", ["chat.run_paused", "chat.approvals.*"])

    await _send(rig, msg.RunPaused(session_id="boot", run_id="r1", requirements=[]))

    assert heard.values("chat.run_paused") == [
        {"session_id": "boot", "run_id": "r1", "requirements": []}
    ]
    assert [wire for wire, _ in heard.got] == ["chat.run_paused"]
    assert rig.model.services.state.approvals.waiting("boot") == []


async def test_an_approval_asked_twice_is_waited_on_once(rig):
    request = msg.HITLRequest(session_id="boot", requirement_id="q1", tool_name="run_shell_command")

    await _send(rig, request, request)

    assert [a.requirement_id for a in rig.model.services.state.approvals.waiting("boot")] == ["q1"]


async def test_resolving_an_approval_nobody_asked_for_is_told_and_changes_nothing(rig):
    heard = _listen(rig, "acme.all", ["chat.approvals.*"])

    await _send(rig, msg.RequirementResolved(session_id="boot", requirement_id="ghost"))

    assert heard.values("chat.approvals.resolved") == [
        {"session_id": "boot", "requirement_id": "ghost"}
    ]
    assert rig.model.services.state.approvals.waiting("boot") == []


async def test_an_approval_change_is_told_as_it_was_dumped_and_before_the_activity(rig):
    heard = _listen(
        rig,
        "acme.all",
        ["chat.approvals.changed", "app.activity_changed"],
    )
    rig.session.processing = True

    rig.model.broadcasts.approval_changed(
        ApprovalChange(
            session_id="boot",
            requirement_id="q1",
            run_id="r1",
            state=ApprovalState.WAITING,
            origin="sub_agent",
            tool_name="run_shell_command",
        )
    )
    await rig.settle()

    assert [wire for wire, _ in heard.got] == ["chat.approvals.changed", "app.activity_changed"]
    assert heard.values("chat.approvals.changed") == [
        {
            "session_id": "boot",
            "requirement_id": "q1",
            "run_id": "r1",
            "state": "waiting",
            "origin": "sub_agent",
            "tool_name": "run_shell_command",
        }
    ]


# ── The router never fails its sender ─────────────────────────────


async def test_a_translator_that_fails_is_logged_and_does_not_reach_the_sender(
    rig, monkeypatch, caplog
):
    heard = _listen(rig, "acme.all", ["chat.run_started", "app.activity_changed"])
    rig.session.processing = True

    def broken(_message: Any) -> None:
        raise RuntimeError("ledger gone")

    monkeypatch.setattr(rig.model.services.state.runs, "started", broken)

    with caplog.at_level(logging.ERROR):
        await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r1"))
    await rig.settle()

    assert "observing RunStarted failed" in caplog.text
    assert any(r.exc_info for r in caplog.records)
    # The handler raised before the activity was looked at, so nothing moved.
    assert heard.got == []


async def test_a_message_nobody_translates_still_moves_the_activity(rig):
    heard = _listen(rig, "acme.all", ["app.activity_changed"])
    rig.session.processing = True

    await _send(rig, msg.StreamingDone(session_id="boot", run_id="r1"))

    assert len(heard.values("app.activity_changed")) == 1


# ── Pushes that do not fit are dropped, and say why ───────────────

#: Every channel whose payload is read through a model, with a payload no model fits.
UNFIT: list[tuple[str, dict[str, Any]]] = [
    ("orchestrate_event", {"type": "visualization_delta", "spec_id": ["x"]}),
    ("orchestrate_event", {"type": "nothing_like_it"}),
    ("session_named", {"name": 5}),
    ("permission_mode_changed", {"mode": ["plan"]}),
    ("notifications_changed", {"counts": "many"}),
    ("workflow_event", {"name": 3}),
    ("visualization_action_dispatched", {"action": ["x"]}),
    ("plan_submitted", {"tasks": 4}),
    ("plan_decided", {"decision": 4}),
    ("process_started", {"pid": "x"}),
    ("process_line", {"pid": "x"}),
    ("process_exited", {"pid": "x"}),
    ("background_process_done", {"pid": "x"}),
    ("http_retry_attempt", {"attempt": "x"}),
    ("http_retry_succeeded", {"attempt_count": "x"}),
    ("file_edited", {"path": 3}),
]


@pytest.mark.parametrize(("channel", "payload"), UNFIT, ids=[c for c, _ in UNFIT])
async def test_a_push_that_fits_no_model_tells_nothing_and_warns(rig, caplog, channel, payload):
    heard = _listen(
        rig,
        "acme.all",
        [
            "chat.*",
            "sessions.*",
            "permissions.*",
            "notifications.*",
            "workflows.*",
            "plan.*",
            "processes.*",
        ],
        workspace="read",
    )

    with caplog.at_level(logging.WARNING):
        await _send(rig, msg.PushNotification(channel=channel, session_id="boot", payload=payload))

    assert heard.got == []
    assert f"the {channel} push does not fit" in caplog.text


async def test_a_session_name_without_a_session_is_told_for_the_push_session(rig):
    heard = _listen(rig, "acme.all", ["sessions.named"])

    await _send(
        rig,
        msg.PushNotification(
            channel="session_named",
            session_id="from-push",
            payload={"session_id": "", "name": "Alpha"},
        ),
        msg.PushNotification(
            channel="session_named",
            session_id="from-push",
            payload={"session_id": "own", "name": "Beta"},
        ),
    )

    assert heard.values("sessions.named") == [
        {"session_id": "from-push", "name": "Alpha"},
        {"session_id": "own", "name": "Beta"},
    ]


async def test_entering_plan_mode_names_no_mode_before_it(rig):
    heard = _listen(rig, "acme.all", ["permissions.mode_changed"])

    await _send(
        rig,
        _push("permission_mode_changed", mode="plan", source="agent"),
        _push("permission_mode_changed", mode="default", previous="plan"),
    )

    assert heard.values("permissions.mode_changed") == [
        {"mode": "plan", "previous": None},
        {"mode": "default", "previous": "plan"},
    ]


async def test_a_view_still_streaming_is_not_a_view_and_a_broken_one_is_dropped(rig, caplog):
    heard = _listen(rig, "acme.all", ["chat.views.shown"])

    with caplog.at_level(logging.WARNING):
        await _send(
            rig,
            _push("orchestrate_event", type="visualization_delta", final=False, spec_id="v1"),
            _push("orchestrate_event", type="visualization_delta", spec_id=["x"]),
        )

    assert heard.got == []
    assert "the orchestrate_event push does not fit VisualizationDeltaPayload" in caplog.text


async def test_unread_notifications_are_told_narrowed_to_what_each_listener_may_see(rig):
    wire = "notifications.changed"
    everyone = _listen(rig, "acme.every", [wire])
    own = _listen(rig, "acme.own", [wire], host=[p for p in EVERYTHING if p != "notifications"])

    await _send(
        rig,
        _push(
            "notifications_changed",
            counts={"unread": 5, "by_source": {"acme.own": 2, "acme.other": 3}},
        ),
    )

    assert everyone.values(wire) == [{"unread": 5, "by_source": {"acme.own": 2, "acme.other": 3}}]
    assert own.values(wire) == [{"unread": 2, "by_source": {"acme.own": 2}}]


# ── A push is read through the model that produced it ─────────────


class _Edit(BaseModel):
    path: str


class _Rename(BaseModel):
    old: str
    new: str


class _Nothing:
    """A model whose failure carries no error to name."""

    __name__ = "_Nothing"

    @staticmethod
    def model_validate(_payload: Any) -> Any:
        raise ValidationError.from_exception_data("_Nothing", [])


def test_a_payload_that_fits_is_read_as_the_model_and_one_that_does_not_is_dropped_with_why(caplog):
    with caplog.at_level(logging.WARNING):
        assert read_push(_Edit, {"path": "a"}, channel="edited") == _Edit(path="a")
        assert caplog.records == []

        assert read_push(_Edit, {}, channel="edited") is None

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "host model: the edited push does not fit _Edit (path: Field required)"
    )


def test_a_payload_wrong_in_a_nested_place_names_the_whole_location(caplog):
    class Outer(BaseModel):
        inner: _Edit

    with caplog.at_level(logging.WARNING):
        assert read_push(Outer, {"inner": {"path": 3}}, channel="deep") is None

    assert "(inner.path: Input should be a valid string)" in caplog.text


def test_the_first_model_that_fits_reads_the_payload_and_the_others_are_not_tried(caplog):
    models = (_Edit, _Rename)

    with caplog.at_level(logging.WARNING):
        assert read_push_from(models, {"old": "a", "new": "b"}, channel="c") == _Rename(
            old="a", new="b"
        )
        assert read_push_from(models, {"path": "p", "old": "a", "new": "b"}, channel="c") == _Edit(
            path="p"
        )

    assert caplog.records == []


def test_a_payload_that_fits_none_of_the_models_is_dropped_with_one_warning_naming_them_all(caplog):
    with caplog.at_level(logging.WARNING):
        assert read_push_from((_Edit, _Rename), {"old": "a"}, channel="c") is None

    (record,) = caplog.records
    # The reason given is the last model's: it is the one tried last.
    assert record.getMessage() == (
        "host model: the c push does not fit _Edit or _Rename (new: Field required)"
    )


def test_no_models_to_try_reads_nothing_and_says_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        assert read_push_from((), {"path": "a"}, channel="c") is None

    assert caplog.records == []


def test_a_failure_with_no_error_to_name_still_says_what_did_not_fit(caplog):
    with caplog.at_level(logging.WARNING):
        assert read_push(_Nothing, {}, channel="odd") is None  # type: ignore[type-var]

    assert [r.getMessage() for r in caplog.records] == [
        "host model: the odd push does not fit _Nothing"
    ]


def test_an_error_without_a_location_is_said_to_be_in_the_payload(caplog):
    class Whole(BaseModel):
        path: str

    class Refuses:
        __name__ = "Refuses"

        @staticmethod
        def model_validate(_payload: Any) -> Any:
            raise ValidationError.from_exception_data(
                "Refuses",
                [{"type": "value_error", "loc": (), "input": {}, "ctx": {"error": "bad"}}],
            )

    with caplog.at_level(logging.WARNING):
        assert read_push(Refuses, {}, channel="whole") is None  # type: ignore[type-var]

    assert "(the payload: Value error, bad)" in caplog.text
    assert read_push(Whole, {"path": "x"}, channel="whole") == Whole(path="x")


def test_a_union_names_the_member_it_fits_and_drops_an_unknown_one(caplog):
    from typing import Annotated, Literal

    from pydantic import Field

    class Started(BaseModel):
        type: Literal["started"]

    class Stopped(BaseModel):
        type: Literal["stopped"]

    adapter = TypeAdapter(Annotated[Started | Stopped, Field(discriminator="type")])

    with caplog.at_level(logging.WARNING):
        assert read_union(adapter, {"type": "stopped"}, channel="ev") == Stopped(type="stopped")
        assert caplog.records == []
        assert read_union(adapter, {"type": "paused"}, channel="ev") is None

    (record,) = caplog.records
    assert record.getMessage().startswith(
        "host model: the ev push does not fit the events it names (the payload: Input tag 'paused'"
    )


# ── A change that implies a value is read again, when someone listens ──


class _Values:
    """The value source: which session each re-read was for."""

    def __init__(self, fail: str | None = None) -> None:
        self.reads: list[tuple[str, str]] = []
        self.fail = fail

    def default_session(self) -> str:
        return "the-default"

    async def account(self, session_id: str) -> Any:
        return self._read("account", session_id)

    async def display(self, session_id: str) -> Any:
        return self._read("display", session_id)

    async def loop(self, session_id: str) -> Any:
        return self._read("loop", session_id)

    def _read(self, what: str, session_id: str) -> dict[str, str]:
        if self.fail == what:
            raise RuntimeError(f"{what} unreadable")
        self.reads.append((what, session_id))
        return {"what": what, "session": session_id}


class _Reads:
    def __init__(self, listening: bool = True, fail: str | None = None) -> None:
        self.told: list[tuple[str, Any]] = []
        self.spawned: list[Coroutine[Any, Any, None]] = []
        self.values = _Values(fail)
        self.reads = DeferredReads(
            wanted=lambda _wire: listening,
            spawn=self.spawned.append,
            emitter=Emitter(lambda wire, value, **_kw: self.told.append((wire, value))),
            values=self.values,
        )

    async def run(self) -> list[BaseException | None]:
        outcomes: list[BaseException | None] = []
        for coroutine in self.spawned:
            try:
                await coroutine
                outcomes.append(None)
            except Exception as exc:
                outcomes.append(exc)
        return outcomes


async def test_the_account_and_the_display_are_read_for_the_default_session_when_none_is_named():
    held = _Reads()

    held.reads.account("")
    held.reads.display("")
    held.reads.account("s7")
    held.reads.display("s8")
    await held.run()

    assert held.values.reads == [
        ("account", "the-default"),
        ("display", "the-default"),
        ("account", "s7"),
        ("display", "s8"),
    ]
    assert [wire for wire, _ in held.told] == [
        "account.changed",
        "app.display_changed",
        "account.changed",
        "app.display_changed",
    ]


async def test_the_loop_is_read_for_exactly_the_session_given_even_an_empty_one():
    held = _Reads()

    held.reads.loop("")
    held.reads.loop("s3")
    await held.run()

    assert held.values.reads == [("loop", ""), ("loop", "s3")]
    assert [wire for wire, _ in held.told] == ["loop.changed", "loop.changed"]


async def test_nothing_is_spawned_or_read_when_nobody_listens():
    held = _Reads(listening=False)

    held.reads.account("s")
    held.reads.display("s")
    held.reads.loop("s")

    assert held.spawned == []
    assert held.values.reads == []
    assert held.told == []


async def test_a_read_that_fails_tells_nothing_and_leaves_the_others_to_be_told():
    held = _Reads(fail="account")

    held.reads.account("s")
    held.reads.loop("s")
    outcomes = await held.run()

    assert isinstance(outcomes[0], RuntimeError)
    assert outcomes[1] is None
    assert [wire for wire, _ in held.told] == ["loop.changed"]


# ── A write made through RPC is told as what it changed ───────────


async def test_each_collection_write_tells_its_own_verb(rig):
    heard = _listen(rig, "acme.all", ["mcp.changed", "plugins.changed", "agents.changed"])
    verbs = {
        RpcMethod.MCP_CONNECT: ("mcp.changed", "connected"),
        RpcMethod.MCP_DISCONNECT: ("mcp.changed", "disconnected"),
        RpcMethod.SET_MCP_TOOL_ENABLED: ("mcp.changed", "tool_toggled"),
        RpcMethod.ENSURE_MCP: ("mcp.changed", "ensured"),
        RpcMethod.INSTALL_PLUGIN: ("plugins.changed", "installed"),
        RpcMethod.UPDATE_PLUGIN: ("plugins.changed", "updated"),
        RpcMethod.REMOVE_PLUGIN: ("plugins.changed", "removed"),
        RpcMethod.SET_PLUGIN_ENABLED: ("plugins.changed", "toggled"),
        RpcMethod.ADD_MARKETPLACE: ("plugins.changed", "marketplace_added"),
        RpcMethod.REMOVE_MARKETPLACE: ("plugins.changed", "marketplace_removed"),
        RpcMethod.REFRESH_MARKETPLACES: ("plugins.changed", "marketplaces_refreshed"),
        RpcMethod.PROMOTE_EPHEMERAL_AGENT: ("agents.changed", "promoted"),
        RpcMethod.DISCARD_EPHEMERAL_AGENT: ("agents.changed", "discarded"),
    }

    for method in verbs:
        rig.model.broadcasts.after_rpc(
            RpcWrite(method=method, args={"name": "thing"}, session_id="boot")
        )
    await rig.settle()

    assert heard.got == [
        (wire, {"what": what, "name": "thing", "payload": None}) for wire, what in verbs.values()
    ]


async def test_a_plugin_write_is_named_by_its_subject_then_its_url_then_its_ref(rig):
    heard = _listen(rig, "acme.all", ["plugins.changed"])
    write = rig.model.broadcasts.after_rpc

    write(
        RpcWrite(
            method=RpcMethod.INSTALL_PLUGIN,
            args={"name": "lint", "url": "https://x.test/p.git", "ref": "r"},
            session_id="boot",
        )
    )
    write(
        RpcWrite(
            method=RpcMethod.INSTALL_PLUGIN,
            args={"url": "https://x.test/p.git", "ref": "r"},
            session_id="boot",
        )
    )
    write(RpcWrite(method=RpcMethod.INSTALL_PLUGIN, args={"ref": "r"}, session_id="boot"))
    write(RpcWrite(method=RpcMethod.REFRESH_MARKETPLACES, args={}, session_id="boot"))
    await rig.settle()

    assert [v["name"] for v in heard.values("plugins.changed")] == [
        "lint",
        "https://x.test/p.git",
        "r",
        None,
    ]


async def test_a_server_write_is_named_by_its_server_when_it_has_no_name(rig):
    heard = _listen(rig, "acme.all", ["mcp.changed", "hooks.changed"])

    rig.model.broadcasts.after_rpc(
        RpcWrite(method=RpcMethod.MCP_DISCONNECT, args={"server_name": "notes"}, session_id="boot")
    )
    rig.model.broadcasts.after_rpc(
        RpcWrite(method=RpcMethod.RELOAD_HOOKS, args={"name": "ignored"}, session_id="boot")
    )
    await rig.settle()

    assert heard.values("mcp.changed")[0]["name"] == "notes"
    assert heard.values("hooks.changed") == [{"what": "reloaded", "name": None, "payload": None}]


async def test_a_history_write_without_its_run_argument_tells_an_empty_run(rig):
    heard = _listen(rig, "acme.all", ["chat.history_changed"])

    rig.model.broadcasts.after_rpc(
        RpcWrite(method=RpcMethod.TRUNCATE_HISTORY, args={"message_id": "m"}, session_id="s9")
    )
    rig.model.broadcasts.after_rpc(
        RpcWrite(method=RpcMethod.DISCARD_INTERRUPTED_RUN, args={"run_id": "r"}, session_id="s9")
    )
    await rig.settle()

    assert heard.values("chat.history_changed") == [
        {"session_id": "s9", "what": "truncated", "run_id": ""},
        {"session_id": "s9", "what": "discarded", "run_id": ""},
    ]


async def test_a_settings_write_naming_no_known_extension_is_told_to_nobody(rig):
    heard = _listen(rig, "acme.all", ["self.settings_changed"])

    rig.model.broadcasts.after_rpc(
        RpcWrite(
            method=RpcMethod.SET_EXTENSION_SETTINGS,
            args={"extension_id": "acme.unknown"},
            session_id="boot",
        )
    )
    await rig.settle()

    assert heard.got == []


async def test_settings_with_unicode_and_nested_values_are_told_as_saved(rig):
    heard = _listen(rig, "acme.all", ["self.settings_changed"])
    rig.store.save_settings(rig.registry.get("acme.all").manifest, {"serviceUrl": "https://s/ünï☃"})

    rig.model.broadcasts.after_rpc(
        RpcWrite(
            method=RpcMethod.SET_EXTENSION_SETTINGS,
            args={"extension_id": "acme.all"},
            session_id="boot",
        )
    )

    assert heard.values("self.settings_changed") == [{"values": {"serviceUrl": "https://s/ünï☃"}}]


async def test_a_switch_of_session_is_told_as_one_in_no_window(rig):
    heard = _listen(rig, "acme.all", ["session.switched"])

    rig.model.broadcasts.after_rpc(
        RpcWrite(method=RpcMethod.SWITCH_SESSION, args={"session_id": "s2"}, session_id="boot")
    )
    await rig.settle()

    assert heard.values("session.switched") == [{"session_id": "s2", "window": None}]


async def test_a_method_with_no_translation_and_a_cancelled_task_are_told_apart(rig):
    heard = _listen(rig, "acme.all", ["scheduler.event", "models.changed"])

    rig.model.broadcasts.after_rpc(
        RpcWrite(method=RpcMethod.LIST_SESSIONS, args={"task_id": "x"}, session_id="boot")
    )
    rig.model.broadcasts.after_rpc(
        RpcWrite(method=RpcMethod.CANCEL_SCHEDULED_TASK, args={"task_id": "c9"}, session_id="boot")
    )
    await rig.settle()

    assert [(wire, v["kind"], v["task_id"]) for wire, v in heard.got] == [
        ("scheduler.event", "cancelled", "c9")
    ]
