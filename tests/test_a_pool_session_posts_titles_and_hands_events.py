"""A session of the pool as an unattended post reaches it, and the handler an event is handed to.

``PoolSession`` is driven over a stub orchestrator that records what the
pool was told, so the busy states, the refusal of a queued attachment, the
title's push and the post's labels are pinned directly. The
``ExtensionEventHandler`` is driven with a hooks spy to pin what it hands
over and what it logs.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.activity import ActivityState
from igni_core.backend.schemas_pool import ExtensionPost
from igni_core.backend.service_event_delivery import ExtensionEventHandler, PoolSession
from igni_core.core.session.schemas import PersistResult
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.extensions.host.hooks import HookFailure
from igni_core.extensions.host.wire import HookAnswer, Outcome
from igni_core.extensions.service_events import EventHandling, ServiceEvent
from igni_core.protocol import messages as msg


class Orchestrator:
    def __init__(self) -> None:
        self.posted: list[ExtensionPost] = []
        self.sent: list[msg.Message] = []
        self.titled: list[str] = []
        self.title_result = PersistResult(ok=True)
        backend = SimpleNamespace(title_session=self._title)
        self.runtime = SimpleNamespace(backend=backend, transport=SimpleNamespace(send=self._send))
        self.pool = SimpleNamespace(find=self._find, get_or_create=self._get_or_create)

    def live(self, state: ActivityState) -> None:
        self.runtime.backend.activity = lambda: SimpleNamespace(state=state)

    def _find(self, session_id: str) -> Any:
        return None if session_id == "gone" else self.runtime

    async def _get_or_create(self, session_id: str) -> Any:
        return self.runtime

    async def _title(self, title: str) -> PersistResult:
        self.titled.append(title)
        return self.title_result

    async def _send(self, message: msg.Message) -> None:
        self.sent.append(message)

    async def post_from_extension(self, post: ExtensionPost) -> None:
        self.posted.append(post)


@pytest.mark.parametrize(
    ("state", "busy"),
    [
        (ActivityState.IDLE, False),
        (ActivityState.RUNNING, True),
        (ActivityState.WAITING, True),
        (None, False),
    ],
)
def test_a_session_is_busy_unless_idle_or_unprobed(state: ActivityState | None, busy: bool) -> None:
    orchestrator = Orchestrator()
    if state is not None:
        orchestrator.live(state)

    assert PoolSession(orchestrator, "s-1").busy() is busy


def test_a_session_not_in_the_pool_is_not_busy() -> None:
    assert PoolSession(Orchestrator(), "gone").busy() is False


async def test_a_post_carries_its_extension_its_service_and_its_media() -> None:
    orchestrator = Orchestrator()
    media = ChatAttachments(files={"/p/a.txt": "x"})

    await PoolSession(orchestrator, "s-1").post(
        extension_id="acme.claims", text="héllo ✓", queued=True, media=media, from_service="svc"
    )

    [post] = orchestrator.posted
    assert (post.session_id, post.extension_id, post.text) == ("s-1", "acme.claims", "héllo ✓")
    assert post.queued and post.from_service == "svc" and post.media == media


async def test_an_idle_session_gets_a_run_and_a_busy_one_a_queued_message() -> None:
    orchestrator = Orchestrator()
    session = PoolSession(orchestrator, "s-1")

    orchestrator.live(ActivityState.IDLE)
    started = await session.deliver(extension_id="e", text="one", media=None, from_service="svc")
    orchestrator.live(ActivityState.RUNNING)
    queued = await session.deliver(extension_id="e", text="two", media=None, from_service="svc")

    assert started.ok and not started.queued and queued.ok and queued.queued
    assert [(p.text, p.queued, p.from_service) for p in orchestrator.posted] == [
        ("one", False, "svc"),
        ("two", True, "svc"),
    ]
    assert orchestrator.posted[0].media == ChatAttachments(), "no media: none carried"


async def test_a_message_queued_for_a_run_cannot_carry_files() -> None:
    orchestrator = Orchestrator()
    orchestrator.live(ActivityState.WAITING)

    refused = await PoolSession(orchestrator, "s-7").deliver(
        extension_id="e", text="see", media=ChatAttachments(files={"/p/a": "x"}), from_service=""
    )

    assert not refused.ok
    assert "session s-7" in refused.reason and "cannot carry attachments" in refused.reason
    assert orchestrator.posted == [], "nothing was posted"


async def test_media_without_files_is_accepted_for_a_queued_message() -> None:
    orchestrator = Orchestrator()
    orchestrator.live(ActivityState.RUNNING)

    answer = await PoolSession(orchestrator, "s-1").deliver(
        extension_id="e", text="see", media=ChatAttachments(), from_service=""
    )

    assert answer.ok and answer.queued


async def test_files_ride_along_with_a_message_that_starts_a_run() -> None:
    orchestrator = Orchestrator()
    orchestrator.live(ActivityState.IDLE)
    media = ChatAttachments(files={"/p/a": "x"})

    answer = await PoolSession(orchestrator, "s-1").deliver(
        extension_id="e", text="see", media=media, from_service=""
    )

    assert answer.ok and orchestrator.posted[0].media == media


async def test_a_title_that_took_tells_every_window_in_one_push() -> None:
    orchestrator = Orchestrator()

    result = await PoolSession(orchestrator, "s-1").title("Claim CL-1")

    assert result.ok and orchestrator.titled == ["Claim CL-1"]
    [push] = orchestrator.sent
    assert isinstance(push, msg.PushNotification)
    assert push.channel == "session_named"
    assert push.payload == {"session_id": "s-1", "name": "Claim CL-1"}


async def test_a_title_that_was_refused_tells_nobody() -> None:
    orchestrator = Orchestrator()
    orchestrator.title_result = PersistResult(ok=False, error="no disk")

    result = await PoolSession(orchestrator, "s-1").title("Claim CL-1")

    assert not result.ok and result.error == "no disk"
    assert orchestrator.sent == []


# ── The handler ───────────────────────────────────────────────────


def _event() -> ServiceEvent:
    return ServiceEvent.model_validate(
        {
            "event_id": "evt-1",
            "extension_id": "acme.indexer",
            "client_id": "svc-indexer",
            "type": "index.ready",
            "payload": {"repo": "acme/api"},
        }
    )


class Hooks:
    def __init__(self, answer: HookAnswer | HookFailure) -> None:
        self.answer = answer
        self.asked: list[tuple[Any, ...]] = []

    async def ask_one(self, extension_id: str, event: str, value: Any, **kwargs: Any) -> HookAnswer:
        self.asked.append((extension_id, event, value, kwargs))
        if isinstance(self.answer, HookFailure):
            raise self.answer
        return self.answer


async def test_the_extension_is_handed_the_typed_event_with_the_handler_s_deadlines() -> None:
    hooks = Hooks(HookAnswer(hook="", outcome=Outcome.ALLOW))
    event = _event()

    handling = await ExtensionEventHandler(hooks)(event)  # type: ignore[arg-type]

    assert handling == EventHandling(handled=True)
    [(extension_id, name, value, kwargs)] = hooks.asked
    assert (extension_id, name) == ("acme.indexer", "ServiceEvent")
    assert value is event, "the event itself, not a dump of it"
    assert kwargs == {"timeout": 10.0, "connect_timeout": 2.0}


async def test_a_reject_without_a_reason_names_the_type() -> None:
    hooks = Hooks(HookAnswer(hook="", outcome=Outcome.REJECT))

    handling = await ExtensionEventHandler(hooks)(_event())  # type: ignore[arg-type]

    assert handling == EventHandling(handled=False, reason="no handler for index.ready")


async def test_a_failed_handler_says_why() -> None:
    hooks = Hooks(HookAnswer(hook="", outcome=Outcome.FAILED, reason="boom"))

    handling = await ExtensionEventHandler(hooks)(_event())  # type: ignore[arg-type]

    assert handling == EventHandling(handled=False, reason="the handler failed: boom")


async def test_an_extension_that_cannot_be_asked_is_not_handled_with_the_reason() -> None:
    hooks = Hooks(HookFailure("acme.indexer is not connected"))

    handling = await ExtensionEventHandler(hooks)(_event())  # type: ignore[arg-type]

    assert handling == EventHandling(handled=False, reason="acme.indexer is not connected")


async def test_text_an_older_handler_returns_is_dropped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hooks = Hooks(HookAnswer(hook="", outcome=Outcome.MODIFY, value={"prompt": "Do it."}))

    with caplog.at_level(logging.WARNING, logger="igni_core.backend.service_event_delivery"):
        handling = await ExtensionEventHandler(hooks)(_event())  # type: ignore[arg-type]

    assert handling == EventHandling(handled=True)
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "acme.indexer" in record.getMessage() and "index.ready" in record.getMessage()
    assert "no longer posts" in record.getMessage()
    assert "Do it." not in record.getMessage()
