"""The /ctx breakdown is all zeros when the stored session cannot be read, and capped runs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.core.session.compaction import ContextBreakdownReporter


def _reporter(load) -> ContextBreakdownReporter:
    source = SimpleNamespace(load=load)
    return ContextBreakdownReporter(source)  # type: ignore[arg-type]


def _msg(role: str, content: str, **metrics):
    return SimpleNamespace(
        role=role,
        content=content,
        metrics=SimpleNamespace(
            input_tokens=metrics.get("i", 0), output_tokens=metrics.get("o", 0)
        ),
    )


@pytest.mark.asyncio
async def test_a_load_that_raises_gives_the_empty_breakdown() -> None:
    result = await _reporter(AsyncMock(side_effect=RuntimeError("db"))).report()

    assert (result.total, result.runs, result.floor) == (0, 0, 0)


@pytest.mark.asyncio
async def test_messages_that_cannot_be_assembled_give_total_zero_and_runs_capped_to_it() -> None:
    agno = SimpleNamespace(
        runs=[SimpleNamespace(messages=[_msg("user", "x" * 400)])],
        get_messages=MagicMock(side_effect=ValueError("bad")),
    )

    result = await _reporter(AsyncMock(return_value=agno)).report()

    assert (result.total, result.runs, result.floor) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runs", [None, "missing", [SimpleNamespace(messages=None)], [SimpleNamespace(messages=[])]]
)
async def test_no_runs_or_runs_without_messages_count_as_no_run_tokens(runs) -> None:
    answer = _msg("assistant", "y" * 40, i=100, o=20)
    agno = SimpleNamespace(get_messages=MagicMock(return_value=[answer]))
    if runs != "missing":
        agno.runs = runs

    result = await _reporter(AsyncMock(return_value=agno)).report()

    assert (result.total, result.runs, result.floor) == (120, 0, 120)


@pytest.mark.asyncio
async def test_runs_are_capped_at_the_total_and_the_floor_is_zero() -> None:
    answer = _msg("assistant", "y" * 4000, i=10, o=0)
    agno = SimpleNamespace(
        runs=[SimpleNamespace(messages=[answer])],
        get_messages=MagicMock(return_value=[answer]),
    )

    result = await _reporter(AsyncMock(return_value=agno)).report()

    assert (result.total, result.runs, result.floor) == (10, 10, 0)


@pytest.mark.asyncio
async def test_the_stored_system_message_is_floor_not_runs() -> None:
    """Agno stores the run's system message among its messages. Counted as
    a turn, it ate the floor: /ctx said 'Floor: 0 tokens' for a session
    whose system prompt alone was thousands (campaign R052, R149)."""
    system = _msg("system", "s" * 40_000)
    user = _msg("user", "u" * 400)
    answer = _msg("assistant", "a" * 400, i=12_000, o=100)
    agno = SimpleNamespace(
        runs=[SimpleNamespace(messages=[system, user, answer])],
        get_messages=MagicMock(return_value=[user, answer]),
    )

    result = await _reporter(AsyncMock(return_value=agno)).report()

    assert result.total == 12_100
    assert result.runs == 200
    assert result.floor == 11_900


@pytest.mark.asyncio
async def test_after_a_compaction_the_floor_is_the_one_kept_plus_the_new_summary() -> None:
    """/compact cleared the runs, and /ctx said 'Total: 0 ... Floor: 0' while
    the next prompt still carried ~18k of floor (campaign R052, R149)."""
    from igni_core.core.session.compaction import ContextBreakdownReporter

    system = _msg("system", "s" * 40_000)
    user = _msg("user", "u" * 400)
    answer = _msg("assistant", "a" * 400, i=12_000, o=100)
    agno = SimpleNamespace(
        runs=[SimpleNamespace(messages=[system, user, answer])],
        session_data={"session_state": {}},
        summary=SimpleNamespace(summary="p" * 400),
    )
    agno.get_messages = lambda: [m for r in agno.runs for m in r.messages if m.role != "system"]

    ContextBreakdownReporter.remember_floor(agno, previous_summary="p" * 400)
    agno.runs = []
    agno.summary = SimpleNamespace(summary="n" * 800)

    result = await _reporter(AsyncMock(return_value=agno)).report()

    # floor 11 900 measured, less the old summary (100), plus the new one (200)
    assert (result.total, result.runs, result.floor) == (12_000, 0, 12_000)
    assert agno.session_data["session_state"] == {}


@pytest.mark.asyncio
async def test_a_session_never_compacted_and_never_run_is_still_empty() -> None:
    agno = SimpleNamespace(runs=[], get_messages=MagicMock(return_value=[]), session_data=None)

    result = await _reporter(AsyncMock(return_value=agno)).report()

    assert (result.total, result.runs, result.floor) == (0, 0, 0)
