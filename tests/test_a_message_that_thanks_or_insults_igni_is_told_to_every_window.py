"""A person's message that thanks, praises or insults igni is pushed as ``user_sentiment``.

Laya decides, beside the run: one choice question, counted only when it
is sure. A long or empty message is never asked about, and a Laya that
cannot answer changes nothing the person sees.
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
    APPRECIATION_THRESHOLD,
    INSULT_THRESHOLD,
    MAX_MESSAGE_CHARS,
    MESSAGE_FIELD,
    SENTIMENT_QUESTION,
)
from igni_core.core.sentiment.judge import SentimentJudge
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel, SentimentKind


class _Laya(Laya):
    """Answers one label at one probability, the rest ``neither``, or raises; records every request."""

    def __init__(
        self,
        answer: str = "gratitude",
        confidence: float = 0.9,
        *,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
        probabilities: dict[str, float] | None = None,
    ) -> None:
        super().__init__()
        rest = {"neither": round(1 - confidence, 6)} if answer != "neither" else {}
        self.probabilities = probabilities or {answer: confidence, **rest}
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
        top = max(self.probabilities, key=self.probabilities.__getitem__)
        answer = DecisionAnswer(
            kind=QuestionKind.CHOICE,
            answer=top,
            confidence=self.probabilities[top],
            probabilities=self.probabilities,
        )
        return [Decision(answers={SENTIMENT_QUESTION.id: answer}, model="laya-test")]


class _Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _Runtime:
    def spawn_auto_name(self, coro: Any) -> None:
        coro.close()


class _Backend:
    def __init__(self) -> None:
        self.runtime = _Runtime()
        self.session_id = "s-1"
        self.runs: list[str] = []

    def consume_plan_research_flag(self) -> bool:
        return False

    async def run_message(self, text: str, media: Any) -> AsyncIterator[Any]:
        self.runs.append(text)
        yield msg.Info(text="working")

    async def maybe_auto_name_session(self) -> AutoNameResult:
        return AutoNameResult(ok=False, reason="already_named")


class _RunThatWaitsForLaya(_Backend):
    """A run that goes on only once Laya has been asked: it deadlocks if the ask waits for it."""

    def __init__(self, laya: _Laya) -> None:
        super().__init__()
        self._laya = laya

    async def run_message(self, text: str, media: Any) -> AsyncIterator[Any]:
        self.runs.append(text)
        await asyncio.wait_for(self._laya.asked.wait(), 2)
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


async def _say(text: str, laya: Laya | None) -> tuple[_Transport, _Backend]:
    dispatcher, transport, backend = _dispatcher(laya)
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


async def test_thanks_is_pushed_with_the_session_and_gratitude_after_the_echo(asks):
    transport, backend = await _say("thank you!", _Laya("gratitude", 0.93))

    assert _ended_cleanly(asks)
    assert _sentiments(transport.sent) == [{"session_id": "s-1", "kind": "gratitude"}]
    assert transport.sent[0].type == "user_message_received"
    assert backend.runs == ["thank you!"]


async def test_praise_is_pushed_as_a_compliment():
    transport, _ = await _say("you're amazing", _Laya("compliment", 0.81))

    assert _sentiments(transport.sent) == [{"session_id": "s-1", "kind": "compliment"}]


async def test_an_insult_is_pushed_as_one(asks):
    transport, backend = await _say("you are useless", _Laya("insult", 0.99))

    assert _ended_cleanly(asks)
    assert _sentiments(transport.sent) == [{"session_id": "s-1", "kind": "insult"}]
    assert backend.runs == ["you are useless"]


async def test_anything_else_is_not_pushed(asks):
    transport, backend = await _say("what's the weather", _Laya("neither", 0.99))

    assert _ended_cleanly(asks)
    assert _sentiments(transport.sent) == []
    assert backend.runs == ["what's the weather"]


async def test_an_unsure_answer_is_not_pushed(asks):
    transport, _ = await _say("thanks I guess", _Laya("gratitude", APPRECIATION_THRESHOLD - 0.01))

    assert _ended_cleanly(asks)
    assert _sentiments(transport.sent) == []


async def test_a_long_message_is_never_asked_about(asks):
    laya = _Laya("gratitude", 0.99)
    transport, backend = await _say("thanks " * 60, laya)

    assert asks == []
    assert laya.requests == []
    assert _sentiments(transport.sent) == []
    assert backend.runs == ["thanks " * 60]


@pytest.mark.parametrize("text", ["", "   \n\t "])
async def test_an_empty_message_is_never_asked_about(text, asks):
    laya = _Laya("gratitude", 0.99)
    transport, _ = await _say(text, laya)

    assert asks == []
    assert laya.requests == []
    assert _sentiments(transport.sent) == []


async def test_a_laya_that_cannot_answer_changes_nothing_the_person_sees(asks):
    laya = _Laya(error=DecisionsUnavailable("igni's decisions service would not start"))
    transport, backend = await _say("thank you!", laya)

    assert _ended_cleanly(asks)
    assert len(laya.requests) == 1
    assert [m.type for m in transport.sent] == ["user_message_received", "info", "stream_end"]
    assert backend.runs == ["thank you!"]


async def test_the_run_does_not_wait_for_laya():
    gate = asyncio.Event()
    laya = _Laya("gratitude", 0.95, gate=gate)
    dispatcher, transport, backend = _dispatcher(laya, _RunThatWaitsForLaya(laya))

    await dispatcher.dispatch(msg.UserMessage(id="r1", text="thank you!", client_id="tab-a"))

    assert backend.runs == ["thank you!"]
    assert [m.type for m in transport.sent] == ["user_message_received", "info", "stream_end"]
    assert transport.sent[1].text == "Laya was asked while the run went on"
    assert _sentiments(transport.sent) == []
    gate.set()
    await background.drain()
    assert _sentiments(transport.sent) == [{"session_id": "s-1", "kind": "gratitude"}]
    assert transport.sent[-1].type == "push_notification"


async def test_the_push_names_the_session_the_message_was_sent_in():
    gate = asyncio.Event()
    dispatcher, transport, backend = _dispatcher(_Laya("gratitude", 0.95, gate=gate))

    await dispatcher.dispatch(msg.UserMessage(id="r1", text="thank you!", client_id="tab-a"))
    backend.session_id = "s-2"
    gate.set()
    await background.drain()

    assert _sentiments(transport.sent) == [{"session_id": "s-1", "kind": "gratitude"}]


async def test_without_a_judge_nothing_is_asked():
    transport, backend = await _say("thank you!", None)

    assert _sentiments(transport.sent) == []
    assert backend.runs == ["thank you!"]


async def test_an_extensions_message_is_not_the_persons_and_is_not_asked_about():
    laya = _Laya("gratitude", 0.99)
    dispatcher, transport, _ = _dispatcher(laya)

    await dispatcher.post_from_extension("acme.thanks", "thank you!", ChatAttachments())
    await background.drain()

    assert laya.requests == []
    assert _sentiments(transport.sent) == []


async def test_a_queued_message_is_not_asked_about():
    laya = _Laya("gratitude", 0.99)
    dispatcher, transport, _ = _dispatcher(laya)

    await dispatcher.dispatch(msg.QueueMessage(text="thank you!", client_id="tab-a"))
    await background.drain()

    assert laya.requests == []


# ── The judge ─────────────────────────────────────────────────────


async def test_the_judge_asks_one_question_about_the_stripped_message():
    laya = _Laya("gratitude", 0.9)

    assert await SentimentJudge(laya).judge("  дякую!  \n") is SentimentKind.GRATITUDE

    [request] = laya.requests
    assert [context.root for context in request.contexts] == [{MESSAGE_FIELD: "дякую!"}]
    assert request.questions == [SENTIMENT_QUESTION]


def test_the_question_is_one_choice_of_gratitude_compliment_insult_or_neither():
    assert SENTIMENT_QUESTION.id == "sentiment"
    assert SENTIMENT_QUESTION.kind is QuestionKind.CHOICE
    assert f"`{MESSAGE_FIELD}`" in SENTIMENT_QUESTION.question
    assert list(SENTIMENT_QUESTION.options or {}) == [
        "gratitude",
        "compliment",
        "insult",
        "neither",
    ]
    assert MESSAGE_FIELD == "message"
    assert (MAX_MESSAGE_CHARS, APPRECIATION_THRESHOLD, INSULT_THRESHOLD) == (280, 0.6, 0.6)


def _probabilities(gratitude: float, compliment: float, insult: float) -> dict[str, float]:
    return {
        "gratitude": gratitude,
        "compliment": compliment,
        "insult": insult,
        "neither": round(1 - gratitude - compliment - insult, 6),
    }


@pytest.mark.parametrize(
    ("probabilities", "expected"),
    [
        (_probabilities(APPRECIATION_THRESHOLD, 0, 0), SentimentKind.GRATITUDE),
        (_probabilities(0, APPRECIATION_THRESHOLD, 0), SentimentKind.COMPLIMENT),
        (_probabilities(1.0, 0, 0), SentimentKind.GRATITUDE),
        (_probabilities(0, 0.99, 0), SentimentKind.COMPLIMENT),
        (_probabilities(0.29, 0.6, 0.06), SentimentKind.COMPLIMENT),
        (_probabilities(0.4, 0.2, 0.1), SentimentKind.GRATITUDE),
        (_probabilities(0.3, 0.3, 0), SentimentKind.GRATITUDE),
        (_probabilities(0.2, 0.4, 0.1), SentimentKind.COMPLIMENT),
        (_probabilities(APPRECIATION_THRESHOLD - 0.001, 0, 0), None),
        (_probabilities(0, APPRECIATION_THRESHOLD - 0.001, 0), None),
        (_probabilities(0.3, 0.299, 0), None),
        (_probabilities(0, 0, INSULT_THRESHOLD), SentimentKind.INSULT),
        (_probabilities(0, 0, 1.0), SentimentKind.INSULT),
        (_probabilities(0, 0, INSULT_THRESHOLD - 0.001), None),
        (_probabilities(0.03, 0.02, 0.44), None),
        (_probabilities(0.03, 0.32, 0.29), None),
        (_probabilities(0, 0, 0), None),
        ({"something Laya made up": 1.0}, None),
    ],
)
async def test_only_a_sure_appreciation_or_insult_counts(probabilities, expected):
    laya = _Laya(probabilities=probabilities)

    assert await SentimentJudge(laya).judge("thanks") is expected


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
async def test_only_a_short_message_with_words_is_asked_about(text, asked):
    laya = _Laya("neither", 0.9)

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
    laya = _Laya("gratitude", 0.99, gate=asyncio.Event())

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
