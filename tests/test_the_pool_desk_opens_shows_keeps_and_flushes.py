"""The producer's chats over the session pool: a new chat, a kept file, a view, a cancel, the held posts.

``PoolChatDesk`` and ``HeldPostFlusher`` are driven over a stub
orchestrator whose pool, runtime, transport and backend record what they
were told. The rig-desk tests prove the lander; these prove the desk.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.activity import ActivityState
from igni_core.backend.schemas_pool import AttachRefused, ExtensionPost
from igni_core.backend.schemas_rpc import AttachSessionResult
from igni_core.backend.service_item_desk import FileNotKept, HeldPostFlusher, PoolChatDesk
from igni_core.core.session.event_log_schema import VisualizationDeltaDraft
from igni_core.core.session.schemas import PersistResult
from igni_core.protocol import messages as msg


class Orchestrator:
    def __init__(self) -> None:
        self.attached: AttachSessionResult | AttachRefused = AttachSessionResult(
            session_id="s-new", project_dir="/p"
        )
        self.attach_calls: list[tuple[str, Any]] = []
        self.titled: list[str] = []
        self.title_result = PersistResult(ok=True)
        self.sent: list[msg.Message] = []
        self.recorded: list[Any] = []
        self.cancelled = 0
        self.uploads: list[tuple[str, str]] = []
        self.upload_result = SimpleNamespace(error="", path="/p/.igni/attachments/a.txt")
        self.state = ActivityState.IDLE
        self.posted: list[ExtensionPost] = []
        self.with_record = True
        self.live = True
        backend = SimpleNamespace(
            title_session=self._title,
            cancel_run=self._cancel,
            record_event=self._record,
            activity=lambda: SimpleNamespace(state=self.state),
            controllers=SimpleNamespace(files=SimpleNamespace(upload_attachment=self._upload)),
        )
        self.backend = backend
        self.runtime = SimpleNamespace(backend=backend, transport=SimpleNamespace(send=self._send))
        self.pool = SimpleNamespace(find=self._find, get_or_create=self._get_or_create)

    def _find(self, session_id: str) -> Any:
        return self.runtime if self.live else None

    async def _get_or_create(self, session_id: str) -> Any:
        if not self.with_record:
            del self.backend.record_event
        return self.runtime

    async def attach(self, session_id: str, project_dir: Any) -> Any:
        self.attach_calls.append((session_id, project_dir))
        return self.attached

    async def _title(self, title: str) -> PersistResult:
        self.titled.append(title)
        return self.title_result

    def _cancel(self) -> None:
        self.cancelled += 1

    async def _record(self, draft: Any) -> None:
        self.recorded.append(draft)

    async def _upload(self, filename: str, content_b64: str) -> Any:
        self.uploads.append((filename, content_b64))
        return self.upload_result

    async def _send(self, message: msg.Message) -> None:
        self.sent.append(message)

    async def post_from_extension(self, post: ExtensionPost) -> None:
        self.posted.append(post)


# ── open_chat ─────────────────────────────────────────────────────


async def test_a_new_chat_is_attached_as_a_window_does_and_titled() -> None:
    orchestrator = Orchestrator()

    chat = await PoolChatDesk(orchestrator).open_chat("Claim CL-1")

    assert chat == "s-new"
    assert orchestrator.attach_calls == [("", None)]
    assert orchestrator.titled == ["Claim CL-1"]


async def test_a_refused_attach_is_an_error_not_a_chat() -> None:
    orchestrator = Orchestrator()
    orchestrator.attached = AttachRefused(reason="not a directory")

    with pytest.raises(RuntimeError, match="a new chat was refused: not a directory"):
        await PoolChatDesk(orchestrator).open_chat("Claim CL-1")
    assert orchestrator.titled == []


async def test_a_title_the_chat_refuses_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    orchestrator = Orchestrator()
    orchestrator.title_result = PersistResult(ok=False, error="read-only")

    with caplog.at_level(logging.INFO, logger="igni_core.backend.service_item_desk"):
        await PoolChatDesk(orchestrator).rename(session_id="s-1", title="New")

    assert "not titled" in caplog.text and "read-only" in caplog.text


# ── keep_file ─────────────────────────────────────────────────────


async def test_a_file_is_kept_through_the_composers_upload_as_base64() -> None:
    orchestrator = Orchestrator()

    path = await PoolChatDesk(orchestrator).keep_file(
        session_id="s-1", filename="réport.txt", content=b"\x00\xffbytes"
    )

    assert path == "/p/.igni/attachments/a.txt"
    [(filename, encoded)] = orchestrator.uploads
    assert filename == "réport.txt" and base64.b64decode(encoded) == b"\x00\xffbytes"


@pytest.mark.parametrize(
    ("error", "path", "said"),
    [("too large", "", "a.txt: too large"), ("", "", "a.txt: not kept"), ("", None, "a.txt")],
)
async def test_a_file_the_chat_will_not_keep_is_refused(error: str, path: Any, said: str) -> None:
    orchestrator = Orchestrator()
    orchestrator.upload_result = SimpleNamespace(error=error, path=path)

    with pytest.raises(FileNotKept, match=said):
        await PoolChatDesk(orchestrator).keep_file(session_id="s-1", filename="a.txt", content=b"")


# ── show ──────────────────────────────────────────────────────────


async def test_a_view_is_pushed_to_the_windows_and_recorded_in_the_chat() -> None:
    orchestrator = Orchestrator()
    view = {"root": "r", "elements": {"r": {"type": "Text", "props": {"text": "Paid ✓"}}}}

    await PoolChatDesk(orchestrator).show(
        session_id="s-1", extension_id="acme.claims", view_id="svc-a1", view=view, title="Paid"
    )

    [push] = orchestrator.sent
    assert isinstance(push, msg.PushNotification) and push.channel == "orchestrate_event"
    assert push.payload["type"] == "visualization_delta"
    assert push.payload["agent_path"] == "ext:acme.claims"
    assert push.payload["spec_id"] == "svc-a1"
    assert json.loads(push.payload["json"]) == view, "dumped by alias: the key is json"
    assert "spec_json" not in push.payload
    assert (push.payload["title"], push.payload["source"]) == ("Paid", "acme.claims")
    [draft] = orchestrator.recorded
    assert isinstance(draft, VisualizationDeltaDraft)
    assert draft.payload.spec_id == "svc-a1" and draft.payload.title == "Paid"
    assert orchestrator.posted == [], "never reaches the model"


async def test_a_view_of_a_backend_that_records_nothing_is_still_pushed() -> None:
    orchestrator = Orchestrator()
    orchestrator.with_record = False

    await PoolChatDesk(orchestrator).show(
        session_id="s-1", extension_id="e", view_id="v", view={}, title=""
    )

    assert len(orchestrator.sent) == 1


# ── cancel, busy ──────────────────────────────────────────────────


async def test_cancel_stops_a_live_chat_s_run_and_leaves_an_absent_one() -> None:
    orchestrator = Orchestrator()
    desk = PoolChatDesk(orchestrator)

    await desk.cancel(session_id="s-1")
    orchestrator.live = False
    await desk.cancel(session_id="s-gone")

    assert orchestrator.cancelled == 1


def test_a_chat_not_in_the_pool_is_neither_busy_nor_waiting() -> None:
    orchestrator = Orchestrator()
    orchestrator.live = False
    desk = PoolChatDesk(orchestrator)

    assert not desk.busy("s-gone") and not desk.waiting_on_approval("s-gone")


async def test_a_queue_is_always_queued_and_marked_as_the_service_s() -> None:
    orchestrator = Orchestrator()

    await PoolChatDesk(orchestrator).queue(
        session_id="s-1", extension_id="e", from_service="svc", text="later"
    )

    [post] = orchestrator.posted
    assert post.queued and post.from_service == "svc" and post.text == "later"


# ── the flusher ───────────────────────────────────────────────────


class Held:
    def __init__(self, *posts: Any) -> None:
        self.posts = list(posts)
        self.released: list[str] = []

    def waiting(self) -> list[Any]:
        return list(self.posts)

    def release(self, event_id: str) -> None:
        self.released.append(event_id)


def _post(event_id: str, session_id: str = "s-1") -> Any:
    return SimpleNamespace(
        event_id=event_id,
        session_id=session_id,
        extension_id="acme.claims",
        client_id="svc",
        text=f"held {event_id}",
    )


async def test_a_held_post_of_a_busy_chat_waits_and_an_idle_one_goes() -> None:
    orchestrator = Orchestrator()
    held = Held(_post("e1"))
    flusher = HeldPostFlusher(held=held, desk=PoolChatDesk(orchestrator))  # type: ignore[arg-type]

    orchestrator.state = ActivityState.RUNNING
    assert await flusher.flush() == 0
    assert held.released == [] and orchestrator.posted == []

    orchestrator.state = ActivityState.IDLE
    assert await flusher.flush() == 1
    assert held.released == ["e1"]
    [post] = orchestrator.posted
    assert (post.text, post.from_service, post.extension_id) == ("held e1", "svc", "acme.claims")


async def test_a_held_post_the_desk_refuses_stays_held() -> None:
    held = Held(_post("e1"))

    class Refusing:
        def busy(self, session_id: str) -> bool:
            return False

        async def deliver(self, **_: Any) -> Any:
            return SimpleNamespace(ok=False)

    flusher = HeldPostFlusher(held=held, desk=Refusing())  # type: ignore[arg-type]

    assert await flusher.flush() == 0
    assert held.released == []


async def test_the_loop_keeps_going_after_a_failing_round_and_stop_ends_it_cleanly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(HeldPostFlusher, "INTERVAL_SECONDS", 0.01)
    rounds: list[int] = []

    class Failing(Held):
        def waiting(self) -> list[Any]:
            rounds.append(1)
            if len(rounds) == 1:
                raise RuntimeError("database locked")
            return []

    flusher = HeldPostFlusher(held=Failing(), desk=PoolChatDesk(Orchestrator()))  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR, logger="igni_core.backend.service_item_desk"):
        flusher.start()
        flusher.start()  # idempotent: one loop
        for _ in range(200):
            if len(rounds) >= 3:
                break
            await asyncio.sleep(0.01)
        stopping = asyncio.ensure_future(flusher.stop())
        done, _ = await asyncio.wait({stopping}, timeout=2)
        assert done, "stop did not end the loop"
        await flusher.stop()  # twice is fine
    seen = len(rounds)
    await asyncio.sleep(0.05)

    assert seen >= 3, "the loop survived the failing round"
    assert len(rounds) == seen, "stop ended the loop"
    assert "not posted this round" in caplog.text


async def test_stop_before_start_is_harmless() -> None:
    await HeldPostFlusher(held=Held(), desk=PoolChatDesk(Orchestrator())).stop()  # type: ignore[arg-type]
