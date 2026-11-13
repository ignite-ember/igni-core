"""The compaction coordinator's hook payloads, statuses and error reporting."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.core.hooks.schemas import HookResult
from igni_core.core.session.compaction import coordinator as coordinator_mod
from igni_core.core.session.schemas import PostCompactHookPayload, PreCompactHookPayload
from tests.session_coordinators import compaction_over


class _NullSummaryManager:
    def __init__(self, model=None) -> None:
        self.model = model

    async def acreate_session_summary(self, session):  # noqa: ANN001
        return None


class _RaisingSummaryManager(_NullSummaryManager):
    async def acreate_session_summary(self, session):  # noqa: ANN001
        raise RuntimeError("provider 503")


class _WritingSummaryManager(_NullSummaryManager):
    async def acreate_session_summary(self, session):  # noqa: ANN001
        session.summary = SimpleNamespace(summary="structured recap")


def _db() -> MagicMock:
    db = MagicMock()
    db.get_runs = AsyncMock(return_value=[])
    db.delete_runs = AsyncMock()
    db.upsert_run = AsyncMock()
    return db


def _conversation(summary=None):
    return SimpleNamespace(
        session_id="sess-1", runs=[SimpleNamespace(), SimpleNamespace()], summary=summary
    )


def _stub(*loads):
    """A session-shaped stub whose stored session answers ``loads`` in turn."""
    s = SimpleNamespace(session_id="sess-1", user_id="user-1")
    s.main_team = SimpleNamespace(
        aget_session=AsyncMock(side_effect=list(loads))
        if len(loads) > 1
        else AsyncMock(return_value=loads[0] if loads else None),
        asave_session=AsyncMock(),
        db=_db(),
        model=SimpleNamespace(),
    )
    s.rebuild_main_team = MagicMock()
    s.hook_executor = SimpleNamespace(
        execute=AsyncMock(return_value=HookResult(should_continue=True))
    )
    return s


def _payloads(s, event):
    return [
        c.kwargs["payload"]
        for c in s.hook_executor.execute.call_args_list
        if c.kwargs["event"] == event
    ]


@pytest.fixture
def structured_nothing(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "SessionSummaryManager", _NullSummaryManager)


@pytest.mark.asyncio
async def test_compact_without_a_stored_session_says_so_and_changes_nothing() -> None:
    s = _stub(None)
    result = await compaction_over(s).compact()

    assert result.ok is False
    assert result.status == "Session not found in DB"
    assert result.error == "Session not found in DB"
    assert result.history_cleared is False
    s.rebuild_main_team.assert_not_called()


@pytest.mark.asyncio
async def test_a_structured_summary_is_used_and_the_team_rebuilt_once(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_mod, "SessionSummaryManager", _WritingSummaryManager)
    agno = _conversation()
    s = _stub(agno)
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="never used")

    result = await coord.compact()

    assert (result.ok, result.summary, result.error) == (True, "structured recap", None)
    assert agno.runs == []
    coord._fallback.summarise.assert_not_called()
    s.rebuild_main_team.assert_called_once()


@pytest.mark.asyncio
async def test_a_raising_structured_summariser_is_reported_while_the_fallback_still_rescues(
    monkeypatch,
) -> None:
    monkeypatch.setattr(coordinator_mod, "SessionSummaryManager", _RaisingSummaryManager)
    agno = _conversation()
    s = _stub(agno)
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="rescued")

    result = await coord.compact()

    assert result.ok is False
    assert result.error == "RuntimeError: provider 503"
    assert result.summary == "rescued"
    assert result.history_cleared is True
    assert agno.runs == []


@pytest.mark.asyncio
async def test_a_raising_fallback_is_prefixed_and_keeps_the_conversation(
    structured_nothing,
) -> None:
    agno = _conversation()
    s = _stub(agno)
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(side_effect=ValueError("no model"))

    result = await coord.compact()

    assert result.error == "fallback: ValueError: no model"
    assert result.status == "fallback: ValueError: no model"
    assert result.history_cleared is False
    assert agno.runs
    s.rebuild_main_team.assert_not_called()


@pytest.mark.asyncio
async def test_a_raising_fallback_does_not_replace_the_structured_error(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_mod, "SessionSummaryManager", _RaisingSummaryManager)
    s = _stub(_conversation())
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(side_effect=ValueError("no model"))

    result = await coord.compact()

    assert result.error == "RuntimeError: provider 503"


@pytest.mark.asyncio
async def test_a_fallback_summary_that_cannot_be_attached_still_stands_in(
    structured_nothing, monkeypatch
) -> None:
    def _refuse(**_kw):
        raise TypeError("cannot attach")

    monkeypatch.setattr(coordinator_mod, "SessionSummary", _refuse)
    agno = _conversation()
    coord = compaction_over(_stub(agno))
    coord._fallback.summarise = AsyncMock(return_value="recap")

    result = await coord.compact()

    assert (result.ok, result.summary, result.history_cleared) == (True, "recap", True)


@pytest.mark.asyncio
async def test_a_fallback_that_repeats_the_old_summary_is_not_a_new_one(
    structured_nothing,
) -> None:
    agno = _conversation(summary=SimpleNamespace(summary="old"))
    coord = compaction_over(_stub(agno))
    coord._fallback.summarise = AsyncMock(return_value="old")

    result = await coord.compact()

    assert result.history_cleared is False
    assert agno.runs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tokens", "window"), [(7999, 10_000), (-5, 10_000), (0, 10_000), (9000, -1)]
)
async def test_below_the_threshold_or_with_nonsense_numbers_nothing_runs(tokens, window) -> None:
    s = _stub(_conversation())

    assert await compaction_over(s).compact_if_needed(tokens, window) is False
    s.hook_executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_auto_compaction_fires_both_hooks_with_the_live_session_id_and_scope(
    structured_nothing,
) -> None:
    s = _stub(_conversation())
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="recap")

    assert await coord.compact_if_needed(8000, 10_000) is True

    (pre,) = _payloads(s, "PreCompact")
    (post,) = _payloads(s, "PostCompact")
    assert isinstance(pre, PreCompactHookPayload)
    assert (pre.session_id, pre.scope, pre.tokens_before) == ("sess-1", "auto", 8000)
    assert isinstance(post, PostCompactHookPayload)
    assert (post.session_id, post.scope, post.tokens_before, post.summary_chars) == (
        "sess-1",
        "auto",
        8000,
        0,
    )


@pytest.mark.asyncio
async def test_the_hooks_see_a_session_id_rotated_after_the_coordinator_was_built(
    structured_nothing,
) -> None:
    s = _stub(_conversation())
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="recap")
    s.session_id = "rotated"

    await coord.compact_if_needed(9000, 10_000)

    assert {p.session_id for p in _payloads(s, "PreCompact") + _payloads(s, "PostCompact")} == {
        "rotated"
    }


@pytest.mark.asyncio
async def test_a_hook_executor_reloaded_between_calls_is_the_one_used(structured_nothing) -> None:
    s = _stub(_conversation())
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="recap")
    old = s.hook_executor
    new = SimpleNamespace(execute=AsyncMock(return_value=HookResult(should_continue=True)))

    async def _swap(**kw):
        s.hook_executor = new
        return HookResult(should_continue=True)

    old.execute = AsyncMock(side_effect=_swap)

    await coord.compact_if_needed(9000, 10_000)

    assert old.execute.await_count == 1  # PreCompact
    assert new.execute.await_args.kwargs["event"] == "PostCompact"


@pytest.mark.asyncio
async def test_manual_compaction_says_what_happened_and_tells_the_hooks(structured_nothing) -> None:
    agno = _conversation()
    s = _stub(agno, agno, agno)
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="five chars!")

    result = await coord.force_compact()

    assert result.ok is True
    assert result.status == "Context compacted. Conversation summarized, history cleared."
    assert result.summary == "five chars!"
    assert result.history_cleared is True
    (pre,) = _payloads(s, "PreCompact")
    (post,) = _payloads(s, "PostCompact")
    assert (pre.scope, pre.tokens_before, pre.session_id) == ("manual", 0, "sess-1")
    assert (post.scope, post.summary_chars, post.session_id) == (
        "manual",
        len("five chars!"),
        "sess-1",
    )


@pytest.mark.asyncio
async def test_manual_compaction_whose_summary_cannot_be_read_back_says_the_text_was_empty(
    structured_nothing,
) -> None:
    agno = _conversation()
    s = _stub(agno, agno, SimpleNamespace(runs=[], summary=None))
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="recap")

    result = await coord.force_compact()

    assert result.ok is False
    assert result.history_cleared is True
    assert result.summary == ""
    assert result.status.startswith("Context cleared, but the summariser returned no text")
    (post,) = _payloads(s, "PostCompact")
    assert post.summary_chars == 0


@pytest.mark.asyncio
async def test_manual_compaction_with_a_summariser_error_and_no_summary_names_the_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(coordinator_mod, "SessionSummaryManager", _RaisingSummaryManager)
    agno = _conversation()
    s = _stub(agno, agno, SimpleNamespace(runs=[], summary=None))
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="recap")

    result = await coord.force_compact()

    assert result.ok is False
    assert result.status == "Context cleared, but the summariser failed: RuntimeError: provider 503"
    assert result.error == "RuntimeError: provider 503"
    assert result.history_cleared is True


@pytest.mark.asyncio
async def test_a_failing_read_back_of_the_summary_leaves_it_empty(structured_nothing) -> None:
    agno = _conversation()
    s = _stub(agno, agno, RuntimeError("db down"))
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="recap")

    result = await coord.force_compact()

    assert result.summary == ""
    assert result.ok is False
    assert result.history_cleared is True


@pytest.mark.asyncio
async def test_a_failing_pre_check_does_not_stop_the_compaction(structured_nothing) -> None:
    agno = _conversation()
    s = _stub(RuntimeError("db hiccup"), agno, agno)
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="recap")

    result = await coord.force_compact()

    assert result.ok is True
    assert result.summary == "recap"


@pytest.mark.asyncio
async def test_a_manual_abort_without_an_error_says_the_summariser_returned_no_text(
    structured_nothing,
) -> None:
    agno = _conversation()
    s = _stub(agno)
    coord = compaction_over(s)
    coord._fallback.summarise = AsyncMock(return_value="")

    result = await coord.force_compact()

    assert result.ok is False
    assert result.history_cleared is False
    assert result.status.startswith("Compaction aborted — the summariser returned no text")
    assert "untouched" in result.status
    assert _payloads(s, "PostCompact") == []
    s.rebuild_main_team.assert_not_called()
