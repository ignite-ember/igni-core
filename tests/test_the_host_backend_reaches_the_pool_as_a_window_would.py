"""The host model's backend over the session pool.

``OrchestratorHostBackend`` is what an extension's ``host.*`` reaches:
an RPC is the handler a window would have reached, an action is a
window's message, an attach is a window's attach. Run here over the real
orchestrator, pool, router and observed transport of :class:`HostRig`;
only the sessions are stand-ins.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend.host_backend import HOST_ID_PREFIX, OrchestratorHostBackend
from igni_core.backend.schemas_pool import ExtensionPost
from igni_core.core.activity import ActivityRegistry
from igni_core.core.session.event_log_schema import VisualizationDeltaPayload
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.extensions.host.values import SessionAttached
from igni_core.extensions.service_events import UnattendedPost
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import HostRig


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[HostRig]:
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def _backend(rig: HostRig) -> OrchestratorHostBackend:
    assert rig.orchestrator is not None
    return OrchestratorHostBackend(
        orchestrator=rig.orchestrator,
        transport=rig.transport,
        project_dir=rig.project,
        client_state=rig.orchestrator.client_state,
        activities=ActivityRegistry(),
    )


def _card(spec_id: str, *, final: bool = True, title: str = "t", spec: str = "{}"):
    return VisualizationDeltaPayload(
        spec_id=spec_id, json=spec, final=final, title=title, source="acme.reviewer"
    )


async def test_the_pool_is_listed_by_session_id(rig: HostRig) -> None:
    backend = _backend(rig)
    assert backend.default_session() == "boot"
    assert backend.live_sessions() == ["boot"]

    assert rig.orchestrator is not None
    await rig.orchestrator.pool.get_or_create("second")

    assert backend.live_sessions() == ["boot", "second"]


async def test_an_rpc_runs_the_handler_the_window_would_have_reached(rig: HostRig) -> None:
    backend = _backend(rig)
    assert rig.orchestrator is not None

    # A synchronous handler on the boot session...
    first = await backend.rpc(RpcMethod.TOGGLE_VERBOSE, {}, session_id="boot")
    assert isinstance(first, bool)
    assert await backend.rpc(RpcMethod.TOGGLE_VERBOSE, {}, session_id="boot") is (not first)
    # ... and one on a session that does not exist yet, which it creates.
    assert rig.orchestrator.pool.find("fresh") is None
    assert isinstance(await backend.rpc(RpcMethod.TOGGLE_VERBOSE, {}, session_id="fresh"), bool)
    assert rig.orchestrator.pool.find("fresh") is not None


async def test_an_rpc_result_is_returned_as_json(rig: HostRig) -> None:
    backend = _backend(rig)

    todos = await backend.rpc(RpcMethod.GET_TODOS, {}, session_id="boot")

    assert isinstance(todos, list)
    status = await backend.rpc(RpcMethod.GET_STATUS, {}, session_id="boot")
    assert isinstance(status, dict)


async def test_an_rpc_the_host_model_answers_itself_is_refused(rig: HostRig) -> None:
    backend = _backend(rig)

    for method in (RpcMethod.HOST_MODEL, RpcMethod.ATTACH_SESSION, RpcMethod.WINDOW_MODEL):
        with pytest.raises(ValueError, match="answered by the host model"):
            await backend.rpc(method, {}, session_id="boot")


async def test_an_rpc_cannot_change_the_callers_arguments(rig: HostRig) -> None:
    backend = _backend(rig)
    assert rig.orchestrator is not None
    runtime = await rig.orchestrator.pool.get_or_create("boot")
    seen: list[dict] = []

    def handler(args: dict) -> dict:
        seen.append(args)
        args["touched"] = True
        return {}

    runtime.rpc_table[RpcMethod.TOGGLE_VERBOSE] = handler
    caller = {"keep": 1}
    await backend.rpc(RpcMethod.TOGGLE_VERBOSE, caller, session_id="boot")

    assert caller == {"keep": 1}
    assert seen[0] is not caller


async def test_an_rpc_waits_for_a_coroutine_handler(rig: HostRig) -> None:
    backend = _backend(rig)
    assert rig.orchestrator is not None
    runtime = await rig.orchestrator.pool.get_or_create("boot")

    async def handler(args: dict) -> dict:
        await asyncio.sleep(0)
        return {"answer": 42}

    runtime.rpc_table[RpcMethod.TOGGLE_VERBOSE] = handler

    assert await backend.rpc(RpcMethod.TOGGLE_VERBOSE, {}, session_id="boot") == {"answer": 42}


async def test_an_action_is_dispatched_into_the_session_without_waiting(rig: HostRig) -> None:
    backend = _backend(rig)

    await backend.act(msg.Command(text="/status"), session_id="boot")
    await rig.settle()

    assert rig.session.commands == ["/status"]


async def test_an_ask_waits_for_the_answer_carrying_its_own_id(rig: HostRig) -> None:
    backend = _backend(rig)
    seen_ids: list[str] = []
    original = rig.transport.expect

    def spy(message_id: str):
        seen_ids.append(message_id)
        return original(message_id)

    rig.transport.expect = spy  # type: ignore[method-assign]

    answer = await backend.ask(msg.Command(text="/status"), session_id="boot", timeout=5)

    assert isinstance(answer, msg.CommandResult)
    assert rig.session.commands == ["/status"]
    [message_id] = seen_ids
    assert message_id.startswith(HOST_ID_PREFIX)
    assert answer.id == message_id
    # The answer was taken back, not shown in the windows.
    assert not [m for m in rig.windows.of(msg.CommandResult) if m.id == message_id]
    # And the id is forgotten afterwards.
    assert message_id not in rig.transport._waiting


async def test_an_ask_nobody_answers_times_out_and_forgets_its_id(rig: HostRig) -> None:
    backend = _backend(rig)
    # A session id the orchestrator refuses to dispatch gives no answer on this id.
    ids: list[str] = []
    original = rig.transport.expect

    def spy(message_id: str):
        ids.append(message_id)
        return original(message_id)

    rig.transport.expect = spy  # type: ignore[method-assign]
    # A message that produces no reply carrying the id: a cancel.
    with pytest.raises(asyncio.TimeoutError):
        await backend.ask(msg.Cancel(), session_id="boot", timeout=0.05)

    [message_id] = ids
    assert message_id not in rig.transport._waiting


async def test_concurrent_asks_get_distinct_ids(rig: HostRig) -> None:
    backend = _backend(rig)
    answers = await asyncio.gather(
        *(backend.ask(msg.Command(text=f"/c{i}"), session_id="boot", timeout=5) for i in range(3))
    )

    assert len({a.id for a in answers}) == 3
    assert sorted(rig.session.commands) == ["/c0", "/c1", "/c2"]


async def test_an_attach_returns_the_session_and_its_folder(rig: HostRig, tmp_path: Path) -> None:
    backend = _backend(rig)
    folder = tmp_path / "other"
    folder.mkdir()

    attached = await backend.attach("s-new", str(folder))

    # The rig's stand-in sessions open in the rig's folder whatever was asked.
    assert attached == SessionAttached(session_id="s-new", project_dir=str(rig.project))


async def test_an_attach_the_window_would_be_refused_raises_value_error(
    rig: HostRig, tmp_path: Path
) -> None:
    backend = _backend(rig)

    with pytest.raises(ValueError, match="not a directory"):
        await backend.attach("s-new", str(tmp_path / "missing"))


async def test_a_shown_view_reaches_the_window_and_the_session_log(rig: HostRig) -> None:
    backend = _backend(rig)
    payload = _card("v1", spec='{"a": 1}')

    await backend.show_view(session_id="boot", payload=payload, run_id="run-9")

    [push] = [p for p in rig.windows.pushes("orchestrate_event")]
    assert push.payload["json"] == '{"a": 1}'
    assert "spec_json" not in push.payload
    assert push.payload["spec_id"] == "v1"
    [event] = rig.session.session_events("visualization_delta")
    assert event.payload.spec_id == "v1"
    assert event.run_id == "run-9"


async def test_a_view_shown_for_a_session_not_yet_live_creates_it(rig: HostRig) -> None:
    backend = _backend(rig)
    assert rig.orchestrator is not None

    await backend.show_view(session_id="later", payload=_card("v"), run_id="r")

    assert rig.orchestrator.pool.find("later") is not None


async def test_views_are_the_latest_final_card_per_spec(rig: HostRig) -> None:
    backend = _backend(rig)
    for payload in (
        _card("a", title="first", spec='{"n": 1}'),
        _card("a", title="second", spec='{"n": 2}'),
        _card("b", final=False),
        _card("", title="no spec id"),
        _card("c", title="third"),
    ):
        await backend.show_view(session_id="boot", payload=payload, run_id="r")

    views = backend.views("boot")

    assert [(v.view_id, v.title, v.session_id) for v in views] == [
        ("a", "second", "boot"),
        ("c", "third", "boot"),
    ]
    assert views[0].spec == {"n": 2}


async def test_views_of_a_session_that_is_not_live_are_none(rig: HostRig) -> None:
    assert _backend(rig).views("nobody") == []


async def test_activity_covers_every_session_of_the_pool(rig: HostRig) -> None:
    backend = _backend(rig)
    assert rig.orchestrator is not None
    await rig.orchestrator.pool.get_or_create("second")

    activity = backend.activity()

    assert sorted(s.session_id for s in activity.sessions) == ["boot", "second"]


async def test_a_session_not_live_has_no_approvals_and_the_default_folder(
    rig: HostRig, tmp_path: Path
) -> None:
    backend = _backend(rig)
    assert backend.waiting_approvals("nobody") == []
    assert backend.project_dir_of("nobody") == rig.project

    assert rig.orchestrator is not None
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    runtime = await rig.orchestrator.pool.get_or_create("away")
    runtime.backend.project_dir = folder
    assert backend.project_dir_of("away") == folder


async def test_post_and_deliver_and_title_reach_the_session(rig: HostRig) -> None:
    backend = _backend(rig)
    assert rig.orchestrator is not None

    await backend.post(
        session_id="boot",
        extension_id="acme.reviewer",
        text="look at this",
        media=ChatAttachments(),
        queued=False,
    )
    await rig.settle()
    assert any("look at this" in text for text, _ in rig.session.ran)

    delivered = await backend.deliver(
        session_id="boot",
        extension_id="acme.reviewer",
        text="an event",
        media=ChatAttachments(files={"a.txt": "A"}),
    )
    await rig.settle()
    assert isinstance(delivered, UnattendedPost)
    assert any("an event" in text for text, _ in rig.session.ran)

    await backend.title(session_id="boot", title="Renamed")
    assert rig.session.titles == ["Renamed"]


async def test_a_broadcast_reaches_the_windows_and_none_is_lost_without_a_loop() -> None:
    sent: list[msg.Message] = []

    class Transport:
        async def send(self, message: msg.Message) -> None:
            sent.append(message)

    backend = OrchestratorHostBackend(
        orchestrator=SimpleNamespace(),  # type: ignore[arg-type]
        transport=Transport(),  # type: ignore[arg-type]
        project_dir=Path("."),
        client_state=SimpleNamespace(),  # type: ignore[arg-type]
        activities=ActivityRegistry(),
    )
    backend.broadcast(msg.Info(text="hello"))
    while backend._tasks:
        await asyncio.sleep(0)

    assert [m.text for m in sent] == ["hello"]
    assert not backend._tasks


def test_a_broadcast_with_no_running_loop_is_dropped_quietly() -> None:
    async def send(message: msg.Message) -> None:
        return None

    backend = OrchestratorHostBackend(
        orchestrator=SimpleNamespace(),  # type: ignore[arg-type]
        transport=SimpleNamespace(send=send),  # type: ignore[arg-type]
        project_dir=Path("."),
        client_state=SimpleNamespace(),  # type: ignore[arg-type]
        activities=ActivityRegistry(),
    )

    backend.broadcast(msg.Info(text="nobody hears"))

    assert not backend._tasks


async def test_a_post_carries_its_attachments_as_a_model_to_the_orchestrator() -> None:
    posted: list[ExtensionPost] = []

    class Orchestrator:
        async def post_from_extension(self, post: ExtensionPost) -> None:
            posted.append(post)

    backend = OrchestratorHostBackend(
        orchestrator=Orchestrator(),  # type: ignore[arg-type]
        transport=SimpleNamespace(),  # type: ignore[arg-type]
        project_dir=Path("."),
        client_state=SimpleNamespace(),  # type: ignore[arg-type]
        activities=ActivityRegistry(),
    )

    await backend.post(
        session_id="s1",
        extension_id="acme.reviewer",
        text="with a file",
        media=ChatAttachments(files={"notes.md": "# hi"}),
        queued=True,
    )

    [post] = posted
    assert (post.session_id, post.extension_id, post.text, post.queued) == (
        "s1",
        "acme.reviewer",
        "with a file",
        True,
    )
    assert post.media == ChatAttachments(files={"notes.md": "# hi"})
