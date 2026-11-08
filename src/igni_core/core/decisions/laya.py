"""Laya, asked over HTTP: the backend's client for igni's decisions service.

Laya answers typed questions — choice, score, yes/no — about a short
text. It brings torch and ~1.7 GB of checkpoints, so it does not run in
the backend: it runs in the decisions service
(:mod:`igni_core.decisions_server`), and this client asks it. Two
callers share the process's one client, :data:`process_laya`:

* the Bypass guard (:mod:`igni_core.core.hooks.bypass_guard`), on the
  approval path, with the ``typed-decisions`` checkpoint it was
  measured on (:meth:`Laya.predict`);
* extensions, through ``host.decide``, with Laya's general checkpoints
  (:meth:`Laya.decide`).

The service puts approvals first, across every backend it serves. Where
it is, is :mod:`~igni_core.core.decisions.endpoints`'s to say.

Anything that keeps an answer from coming back — the service will not
start, cannot be reached, or cannot load the model — is
:class:`~.schemas.DecisionsUnavailable`: never a guess.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from igni_core.core.activity.http import busy_call
from igni_core.core.decisions.endpoints import Connection, DecisionsEndpoint, DecisionsEndpoints
from igni_core.core.decisions.schemas import (
    DecideAnswer,
    DecideCall,
    Decision,
    DecisionQuestion,
    DecisionRequest,
    DecisionsFailure,
    DecisionsUnavailable,
    LayaReading,
    PredictAnswer,
    PredictCall,
    Priority,
)

Answer = TypeVar("Answer", bound=BaseModel)


class Laya:
    """The backend's way to ask Laya: one per process, shared by the guard and extensions."""

    #: A first call may wait for the service to load a checkpoint.
    TIMEOUT = httpx.Timeout(300.0, connect=10.0)

    def __init__(self, endpoint: DecisionsEndpoint | None = None) -> None:
        #: None until first asked: then the one the environment configures.
        self._endpoint = endpoint
        self._endpoint_lock = threading.Lock()

    async def predict(
        self,
        state: dict[str, str],
        questions: list[DecisionQuestion],
        *,
        model: str,
        priority: Priority = Priority.APPROVAL,
    ) -> Decision:
        """Every question's answer from one checkpoint — for a caller that measured its questions on it.

        A context longer than Laya reads is answered on what it reads:
        that is how the caller measured it.
        """
        call = PredictCall(state=state, questions=questions, model=model, priority=priority)
        answer = await self._ask("/v1/predict", call, PredictAnswer)
        return LayaReading(questions).decision(answer.result, allow_truncated=True)

    async def decide(self, request: DecisionRequest) -> list[Decision]:
        """Every context's decision, from Laya's general checkpoints, behind the approvals."""
        answer = await self._ask("/v1/decide", DecideCall(request=request), DecideAnswer)
        reading = LayaReading(request.questions)
        return [reading.decision(result) for result in answer.results]

    async def _ask(self, path: str, call: BaseModel, shape: type[Answer]) -> Answer:
        body = call.model_dump_json()
        for attempt in (1, 2):
            connection = await self._connection()
            try:
                async with (
                    busy_call("decisions"),
                    httpx.AsyncClient(
                        base_url=connection.base_url,
                        transport=connection.transport,
                        timeout=self.TIMEOUT,
                        headers={
                            "authorization": f"Bearer {connection.token}",
                            "content-type": "application/json",
                        },
                    ) as client,
                ):
                    response = await client.post(path, content=body)
            except httpx.ConnectError as exc:
                # A local service that idled out between our check and the call.
                self._where().lost()
                if attempt == 2:
                    raise DecisionsUnavailable(
                        f"igni's decisions service could not be reached ({exc})"
                    ) from exc
                continue
            except httpx.HTTPError as exc:
                raise DecisionsUnavailable(
                    f"igni's decisions service did not answer ({type(exc).__name__})"
                ) from exc
            return self._read(response, shape)
        raise AssertionError("unreachable")

    def _where(self) -> DecisionsEndpoint:
        with self._endpoint_lock:
            if self._endpoint is None:
                self._endpoint = DecisionsEndpoints.configured()
            return self._endpoint

    async def _connection(self) -> Connection:
        # Starting a local service blocks for up to a few seconds.
        return await asyncio.to_thread(lambda: self._where().connection())

    @staticmethod
    def _read(response: httpx.Response, shape: type[Answer]) -> Answer:
        if response.status_code == 200:
            try:
                return shape.model_validate_json(response.content)
            except ValidationError as exc:
                raise DecisionsUnavailable("igni's decisions service answered nonsense") from exc
        try:
            failure = DecisionsFailure.model_validate_json(response.content)
            detail = failure.detail
        except ValidationError:
            detail = f"HTTP {response.status_code}"
        raise DecisionsUnavailable(detail)


#: The process's one Laya, shared by the Bypass guard and extensions, so a
#: desktop backend starts one decisions service, not one per caller. It
#: finds the service on its first call.
process_laya = Laya()


__all__ = ["Laya", "process_laya"]
