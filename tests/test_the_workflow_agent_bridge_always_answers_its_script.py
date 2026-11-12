"""A workflow script blocked on ``agent()`` always gets a reply.

``WorkflowAgentBridge.answer`` runs the script's prompt on the session's team
and replies ``ok=true`` with the agent's content, or ``ok=false`` with why not:
the run gate is closed, no team, a timeout, a crash. The team is the only double
(it is the dependency, not the subject); the gate and the request are real.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.schemas_workflow_events import AgentRequestEvent, AgentRequestPayload
from igni_core.backend.workflow_agent_bridge import WorkflowAgentBridge
from igni_core.core.config.group_policy.gate import RunGate


def _request(payload: dict) -> AgentRequestEvent:
    return AgentRequestEvent(
        ts=1, run_id="wf_1", seq=0, payload=AgentRequestPayload.model_validate(payload)
    )


def _session(arun) -> MagicMock:  # type: ignore[no-untyped-def]
    session = MagicMock()
    session.main_team.arun = arun
    return session


async def test_a_closed_gate_refuses_with_its_reason_and_never_calls_the_team() -> None:
    gate = RunGate()
    gate.close("group policy required and no pack")
    arun = AsyncMock(return_value="never")

    reply = await WorkflowAgentBridge(_session(arun), gate).answer(
        _request({"id": "r1", "prompt": "p"})
    )

    assert reply.ok is False
    assert reply.id == "r1"
    assert reply.error == "group policy required and no pack"
    arun.assert_not_awaited()


async def test_a_session_without_a_team_refuses() -> None:
    session = MagicMock()
    session.main_team = None

    reply = await WorkflowAgentBridge(session, RunGate()).answer(
        _request({"id": "r2", "prompt": "p"})
    )

    assert reply.ok is False
    assert reply.error == "session.main_team not available"


async def test_a_call_that_outlives_its_timeout_is_refused_naming_the_seconds() -> None:
    async def slow(prompt, stream=False):  # type: ignore[no-untyped-def]
        await asyncio.sleep(5)

    reply = await WorkflowAgentBridge(_session(slow), RunGate()).answer(
        _request({"id": "r3", "prompt": "p", "timeout_seconds": 0.05})
    )

    assert reply.ok is False
    assert reply.error == "agent call timed out after 0.05s"


async def test_an_agent_that_raises_is_a_refusal_and_is_logged(caplog) -> None:
    arun = AsyncMock(side_effect=RuntimeError("model unreachable"))

    with caplog.at_level(logging.ERROR):
        reply = await WorkflowAgentBridge(_session(arun), RunGate()).answer(
            _request({"id": "r4", "prompt": "p"})
        )

    assert reply.ok is False
    assert reply.error == "agent call failed: model unreachable"
    assert any("agent bridge failed for wf_1" in r.getMessage() for r in caplog.records)


async def test_the_prompt_is_run_without_streaming() -> None:
    arun = AsyncMock(return_value="x")

    await WorkflowAgentBridge(_session(arun), RunGate()).answer(
        _request({"id": "r5", "prompt": "héllo"})
    )

    arun.assert_awaited_once_with("héllo", stream=False)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (None, None),
        ("text", "text"),
        ({"a": 1}, {"a": 1}),
        ([1, 2], [1, 2]),
        (3, 3),
        (1.5, 1.5),
        (True, True),
        (SimpleNamespace(content="c", output="o"), "c"),
        (SimpleNamespace(content=None, output="o", text="t"), "o"),
        (SimpleNamespace(content=None, output=None, text="t", response="r"), "t"),
        (SimpleNamespace(content=None, output=None, text=None, response="r"), "r"),
        (
            SimpleNamespace(
                messages=[SimpleNamespace(content="first"), SimpleNamespace(content="last")]
            ),
            "last",
        ),
    ],
)
async def test_the_content_comes_out_of_whatever_shape_the_response_has(response, expected) -> None:
    reply = await WorkflowAgentBridge(_session(AsyncMock(return_value=response)), RunGate()).answer(
        _request({"id": "r6", "prompt": "p"})
    )

    assert reply.ok is True
    assert reply.result == expected


async def test_a_response_with_nothing_to_read_is_its_string_form() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque!"

    for response in (Opaque(), SimpleNamespace(messages=[])):
        reply = await WorkflowAgentBridge(
            _session(AsyncMock(return_value=response)), RunGate()
        ).answer(_request({"id": "r7", "prompt": "p"}))

        assert reply.ok is True
        assert reply.result == str(response)
