"""The backend's client of igni's decisions service: what it sends, retries and refuses.

The service is stood in for at the HTTP boundary; the client, its wire
shapes and its endpoint are real. Anything short of an answer is
``DecisionsUnavailable``, never a guess.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from igni_core.core.activity.registry import ActivityRegistry
from igni_core.core.decisions import Laya, Priority
from igni_core.core.decisions import laya as laya_module
from igni_core.core.decisions.endpoints import Connection, DecisionsEndpoints
from igni_core.core.decisions.schemas import (
    DecisionQuestion,
    DecisionRequest,
    DecisionsUnavailable,
    DecisionTooLong,
)

URGENT = {"id": "urgent", "kind": "noul", "question": "Is `text` urgent?"}
REQUEST = DecisionRequest.model_validate(
    {"contexts": [{"text": "it crashes"}], "questions": [URGENT]}
)
QUESTIONS = [DecisionQuestion.model_validate(URGENT)]
GOOD = {"answers": {"urgent": {"noul": 0.8}}, "routing": {"model": "english"}}

Handler = Callable[[httpx.Request], httpx.Response]


class Endpoint:
    """An endpoint that records being lost and serves one connection."""

    def __init__(self, handler: Handler, token: str = "tok") -> None:
        self.handler = handler
        self.token = token
        self.connections = 0
        self.lost_count = 0

    def connection(self) -> Connection:
        self.connections += 1
        return Connection(
            base_url="http://decisions",
            token=self.token,
            transport=httpx.MockTransport(self.handler),
        )

    def lost(self) -> None:
        self.lost_count += 1


def serving(body: Any, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=body)


class TestWhatItSends:
    async def test_decide_posts_the_request_with_the_secret(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"results": [GOOD]})

        [decision] = await Laya(Endpoint(handler, "s3cret")).decide(REQUEST)
        [request] = seen
        assert (request.method, request.url.path) == ("POST", "/v1/decide")
        assert request.headers["authorization"] == "Bearer s3cret"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {"request": REQUEST.model_dump(mode="json")}
        assert decision.answers["urgent"].answer == "yes"
        assert decision.model == "english"

    async def test_predict_posts_state_questions_checkpoint_and_priority(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"result": GOOD})

        await Laya(Endpoint(handler)).predict({"text": "x"}, QUESTIONS, model="typed-decisions")
        [request] = seen
        assert request.url.path == "/v1/predict"
        body = json.loads(request.content)
        assert body["state"] == {"text": "x"}
        assert body["model"] == "typed-decisions"
        assert body["priority"] == Priority.APPROVAL
        assert body["questions"] == [q.model_dump(mode="json") for q in QUESTIONS]

    async def test_an_extension_s_priority_is_sent_as_asked(self) -> None:
        seen: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["priority"])
            return httpx.Response(200, json={"result": GOOD})

        laya = Laya(Endpoint(handler))
        await laya.predict({"text": "x"}, QUESTIONS, model="m", priority=Priority.EXTENSION)
        assert seen == [1]

    async def test_unicode_survives_the_round_trip(self) -> None:
        seen: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"results": [GOOD]})

        request = DecisionRequest.model_validate(
            {"contexts": [{"text": "naïve 日本語 🙂"}], "questions": [URGENT]}
        )
        await Laya(Endpoint(handler)).decide(request)
        assert seen[0]["request"]["contexts"] == [{"text": "naïve 日本語 🙂"}]

    async def test_the_call_holds_the_process_busy_while_it_is_out(self) -> None:
        held: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            held.append([lease.holder for lease in ActivityRegistry.shared().busy()])
            return httpx.Response(200, json={"results": [GOOD]})

        await Laya(Endpoint(handler)).decide(REQUEST)
        assert held == [["decisions"]]
        assert ActivityRegistry.shared().busy() == []


class TestWhatItReads:
    async def test_one_decision_per_context_in_order(self) -> None:
        two = DecisionRequest.model_validate(
            {"contexts": [{"text": "a"}, {"text": "b"}], "questions": [URGENT]}
        )
        low = {"answers": {"urgent": {"noul": 0.1}}}
        decisions = await Laya(Endpoint(serving({"results": [GOOD, low]}))).decide(two)
        assert [d.answers["urgent"].answer for d in decisions] == ["yes", "no"]

    async def test_decide_refuses_what_laya_cut_but_predict_answers_on_what_it_read(self) -> None:
        cut = {**GOOD, "usage": {"truncated": 1, "state_tokens_dropped": 12}}
        with pytest.raises(DecisionTooLong, match="12 tokens"):
            await Laya(Endpoint(serving({"results": [cut]}))).decide(REQUEST)
        decision = await Laya(Endpoint(serving({"result": cut}))).predict(
            {"text": "x"}, QUESTIONS, model="m"
        )
        assert decision.answers["urgent"].answer == "yes"


class TestWhenNothingIsAnswered:
    async def test_a_dropped_connection_is_retried_once_after_the_endpoint_is_told(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                raise httpx.ConnectError("gone", request=request)
            return httpx.Response(200, json={"results": [GOOD]})

        endpoint = Endpoint(handler)
        [decision] = await Laya(endpoint).decide(REQUEST)
        assert decision.answers["urgent"].answer == "yes"
        assert (len(attempts), endpoint.lost_count, endpoint.connections) == (2, 1, 2)

    async def test_a_second_refused_connection_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        endpoint = Endpoint(handler)
        with pytest.raises(DecisionsUnavailable, match=r"could not be reached \(refused\)"):
            await Laya(endpoint).decide(REQUEST)
        assert (endpoint.connections, endpoint.lost_count) == (2, 2)

    @pytest.mark.parametrize(
        "error", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("torn")]
    )
    async def test_any_other_transport_failure_names_its_type_and_is_not_retried(
        self, error: httpx.HTTPError
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise error

        endpoint = Endpoint(handler)
        with pytest.raises(DecisionsUnavailable, match=type(error).__name__):
            await Laya(endpoint).decide(REQUEST)
        assert (endpoint.connections, endpoint.lost_count) == (1, 0)

    async def test_the_wait_is_five_minutes_to_answer_and_ten_seconds_to_connect(self) -> None:
        assert Laya.TIMEOUT.read == 300.0
        assert Laya.TIMEOUT.connect == 10.0

    @pytest.mark.parametrize(
        "body",
        [
            {"results": "no"},
            {"unrelated": 1},
            {"results": [{"answers": {"urgent": {"noul": "very"}}}]},
        ],
    )
    async def test_a_200_that_does_not_fit_the_shape_is_nonsense(self, body: Any) -> None:
        with pytest.raises(DecisionsUnavailable, match="answered nonsense"):
            await Laya(Endpoint(serving(body))).decide(REQUEST)

    async def test_a_200_that_is_not_json_is_nonsense(self) -> None:
        endpoint = Endpoint(lambda request: httpx.Response(200, text="<html>"))
        with pytest.raises(DecisionsUnavailable, match="answered nonsense"):
            await Laya(endpoint).decide(REQUEST)

    async def test_a_result_that_leaves_a_question_out_is_nonsense_not_a_crash(self) -> None:
        silent = {"answers": {}}
        with pytest.raises(DecisionsUnavailable, match="answered nonsense"):
            await Laya(Endpoint(serving({"results": [silent]}))).decide(REQUEST)
        with pytest.raises(DecisionsUnavailable, match="answered nonsense"):
            await Laya(Endpoint(serving({"result": silent}))).predict(
                {"text": "x"}, QUESTIONS, model="m"
            )

    async def test_a_noul_answer_without_its_probability_is_nonsense_not_a_crash(self) -> None:
        bare = {"answers": {"urgent": {}}}
        with pytest.raises(DecisionsUnavailable, match="answered nonsense"):
            await Laya(Endpoint(serving({"results": [bare]}))).decide(REQUEST)

    @pytest.mark.parametrize("error", ["unavailable", "unauthorized"])
    async def test_a_refusal_surfaces_the_service_s_own_detail(self, error: str) -> None:
        body = {"error": error, "detail": "the model could not be loaded"}
        with pytest.raises(DecisionsUnavailable) as raised:
            await Laya(Endpoint(serving(body, 503))).decide(REQUEST)
        assert str(raised.value) == "the model could not be loaded"

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (502, "<html>bad gateway</html>"),
            (500, ""),
            (401, json.dumps({"error": "teapot", "detail": "x"})),
        ],
    )
    async def test_a_refusal_that_is_not_the_service_s_says_the_http_status(
        self, status: int, body: str
    ) -> None:
        endpoint = Endpoint(lambda request: httpx.Response(status, text=body))
        with pytest.raises(DecisionsUnavailable) as raised:
            await Laya(endpoint).decide(REQUEST)
        assert str(raised.value) == f"HTTP {status}"


class TestWhereTheServiceIs:
    async def test_the_environment_s_endpoint_is_found_on_first_use_and_kept(
        self, monkeypatch
    ) -> None:
        found: list[Endpoint] = []

        def configured() -> Endpoint:
            found.append(Endpoint(serving({"results": [GOOD]})))
            return found[0]

        monkeypatch.setattr(DecisionsEndpoints, "configured", staticmethod(configured))
        laya = Laya()
        assert found == []
        await laya.decide(REQUEST)
        await laya.decide(REQUEST)
        assert len(found) == 1
        assert found[0].connections == 2

    async def test_calls_that_start_together_find_it_once(self, monkeypatch) -> None:
        found: list[Endpoint] = []

        def configured() -> Endpoint:
            time.sleep(0.05)
            found.append(Endpoint(serving({"results": [GOOD]})))
            return found[0]

        monkeypatch.setattr(DecisionsEndpoints, "configured", staticmethod(configured))
        laya = Laya()
        await asyncio.gather(*(laya.decide(REQUEST) for _ in range(5)))
        assert len(found) == 1

    async def test_a_service_that_takes_time_to_start_does_not_block_the_event_loop(self) -> None:
        threads: list[str] = []

        class Slow(Endpoint):
            def connection(self) -> Connection:
                threads.append(threading.current_thread().name)
                return super().connection()

        await Laya(Slow(serving({"results": [GOOD]}))).decide(REQUEST)
        assert threads
        assert threads[0] != threading.main_thread().name

    def test_the_guard_and_extensions_share_one_client(self) -> None:
        from igni_core.core.hooks.bypass_judge import HarmJudge

        assert isinstance(laya_module.process_laya, Laya)
        assert laya_module.__all__ == ["Laya", "process_laya"]
        assert HarmJudge()._laya is laya_module.process_laya  # noqa: SLF001
