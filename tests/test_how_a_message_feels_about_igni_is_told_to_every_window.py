"""How a person's message feels about igni is pushed as ``user_sentiment``.

Laya decides, beside the run: two choice questions in one call, counted
only when it is sure. The push names the run the message started and the
run whose reply it reacts to. A long or empty message is never asked
about, and a Laya that cannot answer changes nothing the person sees.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend import app as app_module
from igni_core.backend.message_dispatcher import MessageDispatcher
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.schemas_history import AssistantTurn, StatsTurn, ToolTurn, UserTurn
from igni_core.backend.schemas_sessions import AutoNameResult
from igni_core.core.activity import background
from igni_core.core.decisions.laya import Laya, process_laya
from igni_core.core.decisions.schemas import (
    Decision,
    DecisionAnswer,
    DecisionRequest,
    DecisionsUnavailable,
    DecisionTooLong,
    QuestionKind,
)
from igni_core.core.sentiment import judge as judge_module
from igni_core.core.sentiment.constants import (
    GESTURE_QUESTION,
    INSULT_THRESHOLD,
    MAX_MESSAGE_CHARS,
    MESSAGE_FIELD,
    NEGATIVE_THRESHOLD,
    POSITIVE_THRESHOLD,
    SENTIMENT_QUESTION,
)
from igni_core.core.sentiment.judge import SentimentJudge
from igni_core.core.sentiment.schemas import SentimentVerdict
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel, SentimentKind

THANKS = {"gratitude": 0.95}
COMPLAINT = {"negative": 0.9}


def _answer(probabilities: dict[str, float]) -> DecisionAnswer:
    top = max(probabilities, key=probabilities.__getitem__)
    return DecisionAnswer(
        kind=QuestionKind.CHOICE,
        answer=top,
        confidence=probabilities[top],
        probabilities=probabilities,
    )


class _Laya(Laya):
    """Answers both questions with the probabilities given, or raises; records every request."""

    def __init__(
        self,
        gesture: dict[str, float] | None = None,
        feeling: dict[str, float] | None = None,
        *,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self.gesture = {"neither": 1.0, **(gesture or {})}
        self.feeling = {"neutral": 1.0, **(feeling or {})}
        self.error = error
        self.gate = gate
        self.requests: list[DecisionRequest] = []
        self.asked = asyncio.Event()

    async def decide(self, request: DecisionRequest) -> list[Decision]:
        self.requests.append(request)
        self.asked.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        answers = {
            SENTIMENT_QUESTION.id: _answer(self.feeling),
            GESTURE_QUESTION.id: _answer(self.gesture),
        }
        return [Decision(answers=answers, model="laya-test")]


class _Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _Runtime:
    def spawn_auto_name(self, coro: Any) -> None:
        coro.close()


class _Backend:
    """A run that starts ``r-2`` after an earlier ``r-1``, with the history to match."""

    def __init__(self, frames: list[Any] | None = None, history: list[Any] | None = None) -> None:
        self.runtime = _Runtime()
        self.session_id = "s-1"
        self.runs: list[str] = []
        self.frames = (
            frames
            if frames is not None
            else [
                msg.RunStarted(run_id="sub", parent_run_id="r-2", agent_name="helper"),
                msg.RunStarted(run_id="r-2", agent_name="main"),
                msg.Info(text="working"),
                msg.RunCompleted(run_id="r-2"),
            ]
        )
        self.history = (
            history
            if history is not None
            else [
                UserTurn(run_id="r-1", content="write the parser"),
                AssistantTurn(run_id="r-1", content="done"),
                StatsTurn(run_id="r-1"),
                UserTurn(run_id="r-2", content="thank you!"),
                AssistantTurn(run_id="r-2", content="you're welcome"),
            ]
        )
        self.history_reads: list[str] = []

    def consume_plan_research_flag(self) -> bool:
        return False

    async def run_message(self, text: str, media: Any) -> AsyncIterator[Any]:
        self.runs.append(text)
        for frame in self.frames:
            yield frame

    async def maybe_auto_name_session(self) -> AutoNameResult:
        return AutoNameResult(ok=False, reason="already_named")

    async def get_chat_history(self, session_id: str) -> list[Any]:
        self.history_reads.append(session_id)
        if isinstance(self.history, Exception):
            raise self.history
        return self.history


class _RunThatWaitsForLaya(_Backend):
    """A run that goes on only once Laya has been asked: it deadlocks if the ask waits for it."""

    def __init__(self, laya: _Laya) -> None:
        super().__init__()
        self._laya = laya

    async def run_message(self, text: str, media: Any) -> AsyncIterator[Any]:
        self.runs.append(text)
        await asyncio.wait_for(self._laya.asked.wait(), 2)
        yield msg.RunStarted(run_id="r-2", agent_name="main")
        yield msg.Info(text="Laya was asked while the run went on")


def _dispatcher(
    laya: Laya | None, backend: _Backend | None = None
) -> tuple[MessageDispatcher, _Transport, _Backend]:
    backend = backend or _Backend()
    transport = _Transport()
    dispatcher = MessageDispatcher(
        backend=backend,  # type: ignore[arg-type]
        transport=transport,
        rpc_table={},
        queue=[],
        login=None,
        sentiment=SentimentJudge(laya) if laya is not None else None,
    )
    return dispatcher, transport, backend


def _sentiments(sent: list[Any]) -> list[dict[str, Any]]:
    return [
        m.payload
        for m in sent
        if isinstance(m, msg.PushNotification) and m.channel == PushChannel.USER_SENTIMENT
    ]


def _push(sentiment: str, confidence: float, message: str | None, reply: str | None) -> dict:
    return {
        "session_id": "s-1",
        "sentiment": sentiment,
        "confidence": confidence,
        "message_run_id": message,
        "reply_run_id": reply,
    }


async def _say(
    text: str, laya: Laya | None, backend: _Backend | None = None
) -> tuple[_Transport, _Backend]:
    dispatcher, transport, backend = _dispatcher(laya, backend)
    await dispatcher.dispatch(msg.UserMessage(id="r1", text=text, client_id="tab-a"))
    await background.drain()
    return transport, backend


@pytest.fixture
def asks(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Future[Any]]:
    """Every background ask about a message the dispatcher started."""
    futures: list[asyncio.Future[Any]] = []
    real = background.start

    def start(work: Any, **kwargs: Any) -> asyncio.Future[Any]:
        future = real(work, **kwargs)
        if kwargs.get("holder") == "sentiment":
            futures.append(future)
        return future

    monkeypatch.setattr(background, "start", start)
    return futures


def _ended_cleanly(asks: list[asyncio.Future[Any]]) -> bool:
    return len(asks) == 1 and asks[0].done() and asks[0].exception() is None


# ── The dispatcher ────────────────────────────────────────────────


async def test_thanks_is_pushed_positive_with_its_run_and_the_reply_it_reacts_to(asks):
    transport, backend = await _say("thank you!", _Laya(gesture=THANKS))

    assert _ended_cleanly(asks)
    assert _sentiments(transport.sent) == [_push("positive", 0.95, "r-2", "r-1")]
    assert transport.sent[0].type == "user_message_received"
    assert backend.runs == ["thank you!"]
    assert backend.history_reads == ["s-1"]


async def test_praise_is_pushed_positive_too():
    transport, _ = await _say("you're amazing", _Laya(gesture={"compliment": 0.81}))

    assert _sentiments(transport.sent) == [_push("positive", 0.81, "r-2", "r-1")]


async def test_displeasure_is_pushed_negative(asks):
    transport, backend = await _say("that's not what I asked", _Laya(feeling=COMPLAINT))

    assert _ended_cleanly(asks)
    assert _sentiments(transport.sent) == [_push("negative", 0.9, "r-2", "r-1")]
    assert backend.runs == ["that's not what I asked"]


async def test_an_insult_is_pushed_negative():
    transport, _ = await _say("you are useless", _Laya(gesture={"insult": 0.99}))

    assert _sentiments(transport.sent) == [_push("negative", 0.99, "r-2", "r-1")]


async def test_anything_else_is_not_pushed_and_the_history_is_not_read(asks):
    transport, backend = await _say("what's the weather", _Laya())

    assert _ended_cleanly(asks)
    assert _sentiments(transport.sent) == []
    assert backend.history_reads == []
    assert backend.runs == ["what's the weather"]


async def test_an_unsure_answer_is_not_pushed(asks):
    laya = _Laya(gesture={"gratitude": POSITIVE_THRESHOLD - 0.01})
    transport, _ = await _say("thanks I guess", laya)

    assert _ended_cleanly(asks)
    assert _sentiments(transport.sent) == []


async def test_thanks_at_the_start_of_a_session_reacts_to_no_reply():
    backend = _Backend(history=[UserTurn(run_id="r-2", content="thanks")])
    transport, _ = await _say("thanks", _Laya(gesture=THANKS), backend)

    assert _sentiments(transport.sent) == [_push("positive", 0.95, "r-2", None)]


async def test_the_reply_is_the_last_one_before_the_messages_run():
    backend = _Backend(
        history=[
            AssistantTurn(run_id="r-0", content="hello"),
            UserTurn(run_id="r-1", content="write it"),
            ToolTurn(run_id="r-1"),
            AssistantTurn(run_id="r-1", content="first part"),
            AssistantTurn(run_id="r-1", content="second part"),
            UserTurn(run_id="r-2", content="this is wrong"),
            AssistantTurn(run_id="r-2", content="sorry"),
            UserTurn(run_id="r-3", content="later"),
            AssistantTurn(run_id="r-3", content="a later reply"),
        ]
    )
    transport, _ = await _say("this is wrong", _Laya(feeling=COMPLAINT), backend)

    assert _sentiments(transport.sent) == [_push("negative", 0.9, "r-2", "r-1")]


async def test_a_run_that_never_started_names_no_run_and_reacts_to_the_last_reply():
    backend = _Backend(
        frames=[msg.Info(text="refused before it began")],
        history=[
            AssistantTurn(run_id="r-0", content="hello"),
            AssistantTurn(run_id="", content="an unrecorded reply"),
            UserTurn(run_id="", content="thanks"),
        ],
    )
    transport, _ = await _say("thanks", _Laya(gesture=THANKS), backend)

    assert _sentiments(transport.sent) == [_push("positive", 0.95, None, "r-0")]


async def test_the_runs_id_is_taken_from_its_end_when_its_start_was_not_seen():
    backend = _Backend(frames=[msg.Info(text="working"), msg.RunCompleted(run_id="r-2")])
    transport, _ = await _say("thanks", _Laya(gesture=THANKS), backend)

    assert _sentiments(transport.sent) == [_push("positive", 0.95, "r-2", "r-1")]


async def test_a_run_event_without_an_id_is_passed_over():
    backend = _Backend(
        frames=[msg.RunStarted(run_id="", agent_name="main"), msg.RunCompleted(run_id="r-2")]
    )
    transport, _ = await _say("thanks", _Laya(gesture=THANKS), backend)

    assert _sentiments(transport.sent) == [_push("positive", 0.95, "r-2", "r-1")]


async def test_a_sub_agents_run_is_not_the_messages():
    backend = _Backend(
        frames=[msg.RunStarted(run_id="sub", parent_run_id="r-9", agent_name="helper")],
        history=[AssistantTurn(run_id="sub", content="helper said"), UserTurn(run_id="x")],
    )
    transport, _ = await _say("thanks", _Laya(gesture=THANKS), backend)

    assert _sentiments(transport.sent) == [_push("positive", 0.95, None, "sub")]


async def test_a_history_that_cannot_be_read_still_pushes_with_no_reply(caplog):
    backend = _Backend(history=RuntimeError("the database is gone"))  # type: ignore[arg-type]

    with caplog.at_level(logging.DEBUG, logger="igni_core.backend.message_dispatcher"):
        transport, _ = await _say("thanks", _Laya(gesture=THANKS), backend)

    assert _sentiments(transport.sent) == [_push("positive", 0.95, "r-2", None)]
    assert "the database is gone" in caplog.text


async def test_a_long_message_is_never_asked_about(asks):
    laya = _Laya(gesture=THANKS)
    text = "this is wrong " * 100
    transport, backend = await _say(text, laya)

    assert asks == []
    assert laya.requests == []
    assert _sentiments(transport.sent) == []
    assert backend.runs == [text]


@pytest.mark.parametrize("text", ["", "   \n\t "])
async def test_an_empty_message_is_never_asked_about(text, asks):
    laya = _Laya(gesture=THANKS)
    transport, _ = await _say(text, laya)

    assert asks == []
    assert laya.requests == []
    assert _sentiments(transport.sent) == []


async def test_a_laya_that_cannot_answer_changes_nothing_the_person_sees(asks):
    laya = _Laya(error=DecisionsUnavailable("igni's decisions service would not start"))
    transport, backend = await _say("thank you!", laya)

    assert _ended_cleanly(asks)
    assert len(laya.requests) == 1
    assert [m.type for m in transport.sent] == [
        "user_message_received",
        "run_started",
        "run_started",
        "info",
        "run_completed",
        "stream_end",
    ]
    assert backend.runs == ["thank you!"]


async def test_the_run_does_not_wait_for_laya():
    gate = asyncio.Event()
    laya = _Laya(gesture=THANKS, gate=gate)
    dispatcher, transport, backend = _dispatcher(laya, _RunThatWaitsForLaya(laya))

    await dispatcher.dispatch(msg.UserMessage(id="r1", text="thank you!", client_id="tab-a"))

    assert backend.runs == ["thank you!"]
    assert [m.type for m in transport.sent] == [
        "user_message_received",
        "run_started",
        "info",
        "stream_end",
    ]
    assert transport.sent[2].text == "Laya was asked while the run went on"
    assert _sentiments(transport.sent) == []
    gate.set()
    await background.drain()
    assert _sentiments(transport.sent) == [_push("positive", 0.95, "r-2", "r-1")]
    assert transport.sent[-1].type == "push_notification"


async def test_the_push_names_the_session_the_message_was_sent_in():
    gate = asyncio.Event()
    dispatcher, transport, backend = _dispatcher(_Laya(gesture=THANKS, gate=gate))

    await dispatcher.dispatch(msg.UserMessage(id="r1", text="thank you!", client_id="tab-a"))
    backend.session_id = "s-2"
    gate.set()
    await background.drain()

    assert _sentiments(transport.sent) == [_push("positive", 0.95, "r-2", "r-1")]
    assert backend.history_reads == ["s-1"]


async def test_without_a_judge_nothing_is_asked():
    transport, backend = await _say("thank you!", None)

    assert _sentiments(transport.sent) == []
    assert backend.runs == ["thank you!"]


async def test_an_extensions_message_is_not_the_persons_and_is_not_asked_about():
    laya = _Laya(gesture=THANKS)
    dispatcher, transport, _ = _dispatcher(laya)

    await dispatcher.post_from_extension("acme.thanks", "thank you!", ChatAttachments())
    await background.drain()

    assert laya.requests == []
    assert _sentiments(transport.sent) == []


async def test_a_queued_message_is_not_asked_about():
    laya = _Laya(gesture=THANKS)
    dispatcher, _, _ = _dispatcher(laya)

    await dispatcher.dispatch(msg.QueueMessage(text="thank you!", client_id="tab-a"))
    await background.drain()

    assert laya.requests == []


# ── The judge ─────────────────────────────────────────────────────


async def test_the_judge_asks_both_questions_about_the_stripped_message_in_one_call():
    laya = _Laya(gesture=THANKS)

    verdict = await SentimentJudge(laya).judge("  дякую!  \n")

    assert verdict == SentimentVerdict(sentiment=SentimentKind.POSITIVE, confidence=0.95)
    [request] = laya.requests
    assert [context.root for context in request.contexts] == [{MESSAGE_FIELD: "дякую!"}]
    assert request.questions == [SENTIMENT_QUESTION, GESTURE_QUESTION]


def test_the_questions_and_their_limits():
    assert (SENTIMENT_QUESTION.id, GESTURE_QUESTION.id) == ("sentiment", "gesture")
    assert SENTIMENT_QUESTION.kind is GESTURE_QUESTION.kind is QuestionKind.CHOICE
    assert f"`{MESSAGE_FIELD}`" in SENTIMENT_QUESTION.question
    assert f"`{MESSAGE_FIELD}`" in GESTURE_QUESTION.question
    assert list(SENTIMENT_QUESTION.options or {}) == ["positive", "negative", "neutral"]
    assert list(GESTURE_QUESTION.options or {}) == ["gratitude", "compliment", "insult", "neither"]
    assert MESSAGE_FIELD == "message"
    assert (MAX_MESSAGE_CHARS, POSITIVE_THRESHOLD, NEGATIVE_THRESHOLD, INSULT_THRESHOLD) == (
        1200,
        0.6,
        0.6,
        0.6,
    )


POSITIVE = SentimentKind.POSITIVE
NEGATIVE = SentimentKind.NEGATIVE


@pytest.mark.parametrize(
    ("gesture", "feeling", "expected"),
    [
        ({"gratitude": POSITIVE_THRESHOLD}, {}, (POSITIVE, POSITIVE_THRESHOLD)),
        ({"compliment": POSITIVE_THRESHOLD}, {}, (POSITIVE, POSITIVE_THRESHOLD)),
        ({"gratitude": 0.29, "compliment": 0.6}, {}, (POSITIVE, 0.89)),
        ({"gratitude": 0.3, "compliment": 0.299}, {}, None),
        ({"gratitude": POSITIVE_THRESHOLD - 0.001}, {}, None),
        ({"compliment": POSITIVE_THRESHOLD - 0.001}, {}, None),
        ({}, {"positive": 1.0}, None),
        ({}, {"negative": NEGATIVE_THRESHOLD}, (NEGATIVE, NEGATIVE_THRESHOLD)),
        ({}, {"negative": NEGATIVE_THRESHOLD - 0.001}, None),
        ({"insult": INSULT_THRESHOLD}, {}, (NEGATIVE, INSULT_THRESHOLD)),
        ({"insult": INSULT_THRESHOLD - 0.001}, {}, None),
        ({"insult": 0.99}, {"negative": 0.5}, (NEGATIVE, 0.99)),
        ({"insult": 0.8}, {"negative": 0.92}, (NEGATIVE, 0.92)),
        ({"insult": 0.44}, {"negative": 0.84}, (NEGATIVE, 0.84)),
        ({"gratitude": 0.9}, {"negative": 0.7}, (NEGATIVE, 0.7)),
        ({"gratitude": 0.9}, {"negative": 0.59}, (POSITIVE, 0.9)),
        ({"neither": 1.0}, {"neutral": 1.0}, None),
        ({"something Laya made up": 1.0}, {"also made up": 1.0}, None),
    ],
)
async def test_negative_first_then_positive_and_only_when_sure(gesture, feeling, expected):
    verdict = await SentimentJudge(_Laya(gesture=gesture, feeling=feeling)).judge("words")

    if expected is None:
        assert verdict is None
    else:
        assert verdict is not None
        assert verdict.sentiment is expected[0]
        assert verdict.confidence == pytest.approx(expected[1])


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("x" * MAX_MESSAGE_CHARS, True),
        ("x" * (MAX_MESSAGE_CHARS + 1), False),
        ("  " + "x" * MAX_MESSAGE_CHARS + "  ", True),
        ("x", True),
        ("", False),
        (" \n ", False),
    ],
)
async def test_only_a_message_with_words_that_laya_reads_whole_is_asked_about(text, asked):
    laya = _Laya()

    assert SentimentJudge.worth_asking(text) is asked
    await SentimentJudge(laya).judge(text)
    assert len(laya.requests) == (1 if asked else 0)


@pytest.mark.parametrize(
    "error",
    [
        DecisionsUnavailable("igni's decisions service could not be reached"),
        DecisionTooLong("the context and questions are longer than Laya reads"),
    ],
    ids=["unavailable", "too_long"],
)
async def test_no_answer_from_laya_is_none_said_quietly(error, caplog):
    with caplog.at_level(logging.DEBUG, logger=judge_module.__name__):
        assert await SentimentJudge(_Laya(error=error)).judge("thank you!") is None

    [record] = [r for r in caplog.records if r.name == judge_module.__name__]
    assert record.levelno == logging.DEBUG
    assert str(error) in record.getMessage()


async def test_a_laya_that_takes_too_long_is_no_answer(monkeypatch, caplog):
    monkeypatch.setattr(judge_module, "ANSWER_TIMEOUT_S", 0.01)
    laya = _Laya(gesture=THANKS, gate=asyncio.Event())

    with caplog.at_level(logging.DEBUG, logger=judge_module.__name__):
        assert await SentimentJudge(laya).judge("thank you!") is None

    assert "TimeoutError" in caplog.text


def test_the_judge_asks_the_processs_one_laya_by_default():
    assert SentimentJudge()._laya is process_laya  # noqa: SLF001


# ── The wiring ────────────────────────────────────────────────────


def _wiring(**override: Any) -> PoolWiring:
    fields: dict[str, Any] = {
        name: object()
        for name in (
            "transport",
            "push_bridge",
            "login",
            "supervisors",
            "gate",
            "contributions",
            "prompt_loader",
            "tool_hints",
            "extensions",
        )
    }
    return PoolWiring(**fields, **override)


def _runtime() -> Any:
    class _R:
        backend = object()
        transport = object()
        rpc_table: dict[str, Any] = {}
        queue: list[str] = []

    return _R()


def test_every_dispatcher_of_the_pool_asks_the_wirings_judge():
    judge = SentimentJudge(_Laya())

    dispatcher = _wiring(sentiment=judge).dispatcher_for(_runtime())

    assert dispatcher._sentiment is judge  # noqa: SLF001


def test_a_wiring_without_a_judge_asks_nothing():
    assert _wiring().dispatcher_for(_runtime())._sentiment is None  # noqa: SLF001


def test_the_backend_wires_a_judge_into_its_pool():
    source = Path(app_module.__file__).read_text()
    assert "sentiment=SentimentJudge()," in source
