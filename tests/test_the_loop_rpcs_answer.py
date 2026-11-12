"""The ``/loop``, compaction and learning RPCs answer what the session
holds, through the real loop and context controllers.

``loop_pop`` hands the client the next step (or says the loop finished
or paused, or that there is nothing to pop), ``loop_cancel`` leaves a
paused loop alone, ``loop_status`` reads the session's loop and the
announced total, ``loop_resume`` answers the prompt or ``""``, and
``loop_pause`` says whether there was a loop. ``compact_if_needed``
turns the session's yes or no into ``compacted`` with the summary of
the conversation that replaced the old one, and refuses a call without
its two numbers; ``extract_learnings`` hands the turn over in order.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.rpc_binding import RpcArgsError
from igni_core.backend.rpc_handlers.loop import (
    CompactIfNeededArgs,
    ExtractLearningsArgs,
    LoopRpcHandler,
)
from igni_core.backend.schemas_context import CompactionOutcome
from igni_core.backend.schemas_loop import LoopStatusSnapshot
from igni_core.backend.server_context import ContextController
from igni_core.backend.server_loop import LoopController
from igni_core.core.session.schemas import LoopAdvance
from igni_core.protocol.rpc import RpcMethod


def _session(**loop: object) -> SimpleNamespace:
    state = {
        "paused": False,
        "pending_loop_prompt": None,
        "loop_iteration_index": 0,
        "loop_iterations_remaining": 0,
        "loop_cap_explicit": False,
        "loop_run_id": "",
        "advance_loop": AsyncMock(return_value=None),
        "cancel_loop": AsyncMock(return_value=True),
        "pause_loop": AsyncMock(return_value=True),
        "resume_loop": AsyncMock(return_value=None),
    } | loop
    return SimpleNamespace(
        loop=SimpleNamespace(**state),
        loop_progress_store=SimpleNamespace(get_announced_total=AsyncMock(return_value=None)),
        compaction=SimpleNamespace(compact_if_needed=AsyncMock(return_value=False)),
        main_team=SimpleNamespace(aget_session=AsyncMock(return_value=None)),
        session_id="s-1",
        user_id="u-1",
        learning_mgr=SimpleNamespace(extract=AsyncMock()),
    )


def _handler(session: SimpleNamespace) -> LoopRpcHandler:
    loop = LoopController(session, None, MagicMock())  # type: ignore[arg-type]
    context = ContextController(session, MagicMock(), MagicMock(), MagicMock(), MagicMock())  # type: ignore[arg-type]
    backend = SimpleNamespace(
        controllers=SimpleNamespace(loop=loop, context=context),
        loop_pause=loop.pause,
        loop_status=loop.status,
    )
    return LoopRpcHandler(SimpleNamespace(backend=backend))  # type: ignore[arg-type]


async def _call(handler: LoopRpcHandler, method: RpcMethod, wire: dict | None = None):
    return await handler.methods()[method](wire or {})


async def test_a_pop_with_no_loop_answers_null() -> None:
    assert await _call(_handler(_session()), RpcMethod.POP_PENDING_LOOP_ITERATION) is None


async def test_a_pop_answers_the_next_step_of_the_loop() -> None:
    step = LoopAdvance(
        kind="step", prompt="go on", display_prompt="/loop go on", iteration=2, remaining=3
    )
    session = _session(advance_loop=AsyncMock(return_value=step))

    answer = await _call(_handler(session), RpcMethod.POP_PENDING_LOOP_ITERATION)

    assert answer == step


@pytest.mark.parametrize("kind", ["completed", "safety_paused"])
async def test_a_pop_says_when_the_loop_finished_or_paused_itself(kind: str) -> None:
    advance = LoopAdvance(kind=kind)  # type: ignore[arg-type]
    session = _session(advance_loop=AsyncMock(return_value=advance))

    answer = await _call(_handler(session), RpcMethod.POP_PENDING_LOOP_ITERATION)

    assert answer.kind == kind


async def test_a_running_loop_is_cancelled() -> None:
    session = _session()

    assert await _call(_handler(session), RpcMethod.CANCEL_PENDING_LOOP) is True
    session.loop.cancel_loop.assert_awaited_once_with()


async def test_a_paused_loop_is_not_cancelled() -> None:
    session = _session(paused=True)

    assert await _call(_handler(session), RpcMethod.CANCEL_PENDING_LOOP) is False
    session.loop.cancel_loop.assert_not_awaited()


async def test_no_loop_to_cancel_answers_false() -> None:
    session = _session(cancel_loop=AsyncMock(return_value=False))

    assert await _call(_handler(session), RpcMethod.CANCEL_PENDING_LOOP) is False


async def test_the_status_reads_the_loop_and_the_total_the_agent_announced() -> None:
    session = _session(
        pending_loop_prompt="tidy up",
        loop_iteration_index=4,
        loop_iterations_remaining=6,
        loop_cap_explicit=True,
        loop_run_id="run-1",
    )
    session.loop_progress_store.get_announced_total.return_value = 10

    status = await _call(_handler(session), RpcMethod.LOOP_STATUS)

    assert status == LoopStatusSnapshot(
        active=True,
        paused=False,
        prompt="tidy up",
        iteration_index=4,
        iterations_remaining=6,
        cap_explicit=True,
        announced_total=10,
    )
    session.loop_progress_store.get_announced_total.assert_awaited_once_with("run-1")


async def test_the_status_with_no_loop_is_inactive() -> None:
    status = await _call(_handler(_session()), RpcMethod.LOOP_STATUS)

    assert (status.active, status.paused, status.prompt, status.announced_total) == (
        False,
        False,
        "",
        None,
    )


async def test_resuming_a_paused_loop_answers_the_prompt_to_send() -> None:
    session = _session(resume_loop=AsyncMock(return_value="next prompt"))

    assert await _call(_handler(session), RpcMethod.LOOP_RESUME) == "next prompt"


async def test_resuming_with_no_paused_loop_answers_an_empty_string() -> None:
    assert await _call(_handler(_session()), RpcMethod.LOOP_RESUME) == ""


@pytest.mark.parametrize("had_loop", [True, False])
async def test_pausing_says_whether_there_was_a_loop(had_loop: bool) -> None:
    session = _session(pause_loop=AsyncMock(return_value=had_loop))

    assert await _call(_handler(session), RpcMethod.LOOP_PAUSE) is had_loop


async def test_a_conversation_that_needs_no_compaction_says_so() -> None:
    session = _session()

    answer = await _call(
        _handler(session), RpcMethod.COMPACT_IF_NEEDED, {"ctx_tokens": 10, "max_ctx": 100}
    )

    assert answer == CompactionOutcome(compacted=False)
    session.compaction.compact_if_needed.assert_awaited_once_with(10, 100)


async def test_a_compacted_conversation_answers_the_summary_that_replaced_it() -> None:
    session = _session()
    session.compaction.compact_if_needed.return_value = True
    session.main_team.aget_session.return_value = SimpleNamespace(
        summary=SimpleNamespace(summary="We fixed the build.")
    )

    answer = await _call(
        _handler(session), RpcMethod.COMPACT_IF_NEEDED, {"ctx_tokens": 90, "max_ctx": 100}
    )

    assert answer == CompactionOutcome(
        compacted=True, new_session_id="s-1", summary="We fixed the build."
    )
    session.compaction.compact_if_needed.assert_awaited_once_with(90, 100)


async def test_compaction_ignores_keys_it_does_not_know() -> None:
    args = CompactIfNeededArgs.model_validate({"ctx_tokens": 1, "max_ctx": 2, "newer": "key"})

    assert (args.ctx_tokens, args.max_ctx) == (1, 2)


@pytest.mark.parametrize(
    "wire",
    [{"ctx_tokens": 1}, {"max_ctx": 1}, {"ctx_tokens": "many", "max_ctx": 1}, {}],
)
async def test_compaction_without_its_two_numbers_is_refused(wire: dict) -> None:
    session = _session()

    with pytest.raises(RpcArgsError):
        await _call(_handler(session), RpcMethod.COMPACT_IF_NEEDED, wire)

    session.compaction.compact_if_needed.assert_not_awaited()


async def test_a_turn_is_handed_to_the_learning_pipeline_user_message_first() -> None:
    session = _session()

    answer = await _call(
        _handler(session),
        RpcMethod.EXTRACT_LEARNINGS,
        {"user_msg": "use tabs", "assistant_msg": "Noted."},
    )

    assert answer is None
    session.learning_mgr.extract.assert_awaited_once_with("use tabs", "Noted.")


async def test_learning_from_a_turn_without_the_user_message_is_refused() -> None:
    session = _session()

    with pytest.raises(RpcArgsError, match="user_msg"):
        await _call(_handler(session), RpcMethod.EXTRACT_LEARNINGS, {"assistant_msg": "Noted."})

    session.learning_mgr.extract.assert_not_awaited()


def test_learning_ignores_keys_it_does_not_know() -> None:
    args = ExtractLearningsArgs.model_validate({"user_msg": "a", "assistant_msg": "b", "x": 1})

    assert (args.user_msg, args.assistant_msg) == ("a", "b")
