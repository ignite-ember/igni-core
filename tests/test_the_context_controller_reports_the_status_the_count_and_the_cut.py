"""What the context controller answers: the status-bar snapshot with its group
theme, the token count and its latch, a compaction's echo, the cut of a
session's history in its own table, and the pending and interrupted rows a
failing store turns into nothing instead of a crash."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agno.db.base import SessionType
from agno.db.sqlite import AsyncSqliteDb
from agno.models.message import Message
from agno.run.agent import RunInput, RunOutput
from agno.session.agent import AgentSession
from sqlalchemy.exc import SQLAlchemyError

from igni_core.backend.schemas_context import PENDING_STALENESS_SECONDS
from igni_core.backend.server_context import ContextController
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.meta import PackMeta, PackMetaStore
from igni_core.core.session.compaction.context_budget import ContextBudget
from igni_core.core.session.pending_messages import PendingMessage

SESSION = "sess-ctx"


class _Layout:
    def __init__(self, root: Path) -> None:
        self._root = root

    def file(self, name: str) -> Path:
        return self._root / name


def _meta_store(tmp_path: Path, meta: PackMeta | None = None) -> PackMetaStore:
    store = PackMetaStore(_Layout(tmp_path / "pack"))  # type: ignore[arg-type]
    if meta is not None:
        store.save(meta)
    return store


def _controller(
    tmp_path: Path,
    *,
    session: SimpleNamespace | None = None,
    pending_store: object | None = None,
    pack_meta: PackMetaStore | None = None,
    models_usable: bool = True,
) -> ContextController:
    if session is None:
        session = _session()
    return ContextController(
        session=session,  # type: ignore[arg-type]
        settings=SimpleNamespace(  # type: ignore[arg-type]
            models=SimpleNamespace(default="main-model", max_context_window=200_000)
        ),
        pending_store=pending_store,  # type: ignore[arg-type]
        models=SimpleNamespace(has_usable_model=lambda: models_usable),  # type: ignore[arg-type]
        pack_meta=pack_meta if pack_meta is not None else _meta_store(tmp_path),
    )


def _session(**over: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "session_id": SESSION,
        "user_id": "u",
        "cloud_auth": SimpleNamespace(connected=False, org_name=None, email=None),
        "context_budget": ContextBudget(window=1000),
        "permission_mode_value": "default",
        "main_team": SimpleNamespace(),
    }
    base.update(over)
    return SimpleNamespace(**base)


# ── the status bar ──────────────────────────────────────────────────


def test_the_status_reports_the_model_the_cloud_the_tokens_and_the_mode(tmp_path: Path) -> None:
    budget = ContextBudget(window=1000)
    budget.latch(321)
    session = _session(
        cloud_auth=SimpleNamespace(connected=True, org_name="Acme", email="ada@acme.io"),
        context_budget=budget,
        permission_mode_value="plan",
    )

    status = _controller(tmp_path, session=session, models_usable=False).get_status()

    assert status.model == "main-model"
    assert status.model_configured is False
    assert (status.cloud_connected, status.cloud_org, status.cloud_email) == (
        True,
        "Acme",
        "ada@acme.io",
    )
    assert status.context_tokens == 321
    assert status.max_context == 200_000
    assert status.permission_mode == "plan"


def test_a_signed_out_status_has_empty_cloud_strings(tmp_path: Path) -> None:
    status = _controller(tmp_path).get_status()

    assert (status.cloud_connected, status.cloud_org, status.cloud_email) == (False, "", "")


def test_the_status_carries_the_group_theme_as_the_pack_declares_it(tmp_path: Path) -> None:
    meta = PackMeta.model_validate({"theme": {"accent": "#123456", "wordmark": "Acme"}})

    status = _controller(tmp_path, pack_meta=_meta_store(tmp_path, meta)).get_status()

    assert status.theme == {"accent": "#123456", "wordmark": "Acme"}


@pytest.mark.parametrize("meta", [None, PackMeta(), PackMeta.model_validate({"theme": {}})])
def test_no_theme_is_none_whether_the_pack_is_missing_themeless_or_empty(
    tmp_path: Path, meta: PackMeta | None
) -> None:
    status = _controller(tmp_path, pack_meta=_meta_store(tmp_path, meta)).get_status()

    assert status.theme is None


def test_an_unreadable_pack_meta_file_is_no_theme(tmp_path: Path) -> None:
    store = _meta_store(tmp_path)
    (tmp_path / "pack").mkdir()
    (tmp_path / "pack" / GroupPolicyLayout.META_FILE).write_text("{not json")

    assert _controller(tmp_path, pack_meta=store).get_status().theme is None


# ── the token count ─────────────────────────────────────────────────


class _Stored:
    """An Agno session as the controller reads it."""

    def __init__(self, messages: list[Message], summary: str | None = None) -> None:
        self._messages = messages
        self.summary = SimpleNamespace(summary=summary) if summary is not None else None

    def get_messages(self) -> list[Message]:
        return self._messages


def _team(stored: object | None = None, error: Exception | None = None) -> SimpleNamespace:
    async def aget_session(**_: object) -> object | None:
        if error is not None:
            raise error
        return stored

    return SimpleNamespace(aget_session=aget_session)


async def test_the_count_is_the_estimate_of_the_stored_messages_and_it_is_latched(
    tmp_path: Path,
) -> None:
    session = _session(main_team=_team(_Stored([Message(role="user", content="x" * 400)])))

    n = await _controller(tmp_path, session=session).count_context_tokens()

    assert n == 100
    assert session.context_budget.last_input_tokens == 100


async def test_a_count_of_zero_does_not_overwrite_the_latched_figure(tmp_path: Path) -> None:
    session = _session(main_team=_team(_Stored([])))
    session.context_budget.latch(55)

    n = await _controller(tmp_path, session=session).count_context_tokens()

    assert n == 0
    assert session.context_budget.last_input_tokens == 55


async def test_a_session_not_stored_counts_zero(tmp_path: Path) -> None:
    session = _session(main_team=_team(None))

    assert await _controller(tmp_path, session=session).count_context_tokens() == 0


async def test_a_storage_failure_counts_zero_instead_of_raising(tmp_path: Path) -> None:
    session = _session(main_team=_team(error=SQLAlchemyError("db down")))
    session.context_budget.latch(55)

    assert await _controller(tmp_path, session=session).count_context_tokens() == 0
    assert session.context_budget.last_input_tokens == 55


# ── compaction ──────────────────────────────────────────────────────


def _compaction(did: bool) -> SimpleNamespace:
    async def compact_if_needed(ctx: int, max_ctx: int) -> bool:
        return did

    return SimpleNamespace(compact_if_needed=compact_if_needed)


async def test_nothing_compacted_is_no_answer(tmp_path: Path) -> None:
    session = _session(compaction=_compaction(False), main_team=_team(_Stored([], "s")))

    assert await _controller(tmp_path, session=session).compact_if_needed(10, 100) is None


async def test_a_compaction_answers_with_the_new_summary(tmp_path: Path) -> None:
    session = _session(compaction=_compaction(True), main_team=_team(_Stored([], "the gist")))

    cleared = await _controller(tmp_path, session=session).compact_if_needed(90, 100)

    assert cleared is not None
    assert (cleared.new_session_id, cleared.summary) == (SESSION, "the gist")


async def test_a_compaction_whose_summary_cannot_be_read_still_answers_with_none(
    tmp_path: Path,
) -> None:
    session = _session(
        compaction=_compaction(True), main_team=_team(error=SQLAlchemyError("db down"))
    )

    cleared = await _controller(tmp_path, session=session).compact_if_needed(90, 100)

    assert cleared is not None and cleared.summary == ""


async def test_a_compaction_with_no_summary_answers_an_empty_one(tmp_path: Path) -> None:
    session = _session(compaction=_compaction(True), main_team=_team(_Stored([])))

    cleared = await _controller(tmp_path, session=session).compact_if_needed(90, 100)

    assert cleared is not None and cleared.summary == ""


async def test_a_finished_turn_goes_to_the_learning_manager(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    async def extract(user: str, assistant: str) -> None:
        seen.append((user, assistant))

    session = _session(learning_mgr=SimpleNamespace(extract=extract))

    await _controller(tmp_path, session=session).extract_learnings("q", "a")

    assert seen == [("q", "a")]


# ── cutting the history ─────────────────────────────────────────────


def _run(index: int, *, parent: str | None = None) -> RunOutput:
    return RunOutput(
        run_id=f"run-{index}",
        agent_id="igni",
        session_id=SESSION,
        parent_run_id=parent,
        input=RunInput(input_content=f"question {index}"),
        messages=[Message(role="user", content=f"question {index}")],
        content=f"answer {index}",
    )


class _Agent:
    """Agno's agent as far as saving a session goes, over its real storage."""

    def __init__(self, db: AsyncSqliteDb, *, fail_save: bool = False) -> None:
        self.db = db
        self._fail_save = fail_save

    async def aget_session(self, *, session_id: str, user_id: str | None = None):
        return await self.db.get_session(session_id=session_id, session_type=SessionType.AGENT)

    async def asave_session(self, session: AgentSession) -> None:
        if self._fail_save:
            raise RuntimeError("disk full")
        await self.db.upsert_session(session)


async def _stored(tmp_path: Path, runs: list[RunOutput]) -> AsyncSqliteDb:
    from igni_core.core.session.persistence.run_history import RunHistory

    db = AsyncSqliteDb(
        db_file=str(tmp_path / "state.db"),
        session_table="igni_sessions",
        memory_table="igni_memories",
    )
    session = AgentSession(
        session_id=SESSION,
        agent_id="igni",
        runs=runs,
        session_data={},
        created_at=int(time.time()),
    )
    await RunHistory(db).save(session)
    return db


async def _run_ids(db: AsyncSqliteDb) -> list[str]:
    stored = await db.get_session(session_id=SESSION, session_type=SessionType.AGENT)
    assert stored is not None
    return [r.run_id for r in stored.runs or []]


async def test_cutting_at_a_run_drops_it_and_every_later_one_from_the_stored_history(
    tmp_path: Path,
) -> None:
    db = await _stored(tmp_path, [_run(0), _run(1), _run(2, parent="run-1"), _run(3)])
    session = _session(main_team=_Agent(db))
    session.context_budget.latch(500)

    result = await _controller(tmp_path, session=session).truncate_history(SESSION, "run-1")

    assert (result.removed, result.error) == (3, "")
    assert await _run_ids(db) == ["run-0"]
    assert session.context_budget.last_input_tokens == 0


async def test_cutting_at_the_first_run_empties_the_history(tmp_path: Path) -> None:
    db = await _stored(tmp_path, [_run(0), _run(1)])
    session = _session(main_team=_Agent(db))

    result = await _controller(tmp_path, session=session).truncate_history(SESSION, "run-0")

    assert result.removed == 2
    assert await _run_ids(db) == []


async def test_a_child_run_is_never_a_cut_point(tmp_path: Path) -> None:
    db = await _stored(tmp_path, [_run(0), _run(1, parent="run-0")])
    session = _session(main_team=_Agent(db))
    session.context_budget.latch(500)

    result = await _controller(tmp_path, session=session).truncate_history(SESSION, "run-1")

    assert result.removed == 0
    assert "run-1" in result.error
    assert await _run_ids(db) == ["run-0", "run-1"]
    assert session.context_budget.last_input_tokens == 500


async def test_cutting_at_an_unknown_run_says_which_and_changes_nothing(tmp_path: Path) -> None:
    db = await _stored(tmp_path, [_run(0)])
    session = _session(main_team=_Agent(db))

    result = await _controller(tmp_path, session=session).truncate_history(SESSION, "nope")

    assert (result.removed, result.error) == (0, "run_id 'nope' not in session")
    assert await _run_ids(db) == ["run-0"]


async def test_cutting_an_unknown_session_says_so(tmp_path: Path) -> None:
    db = await _stored(tmp_path, [_run(0)])
    session = _session(main_team=_Agent(db))

    result = await _controller(tmp_path, session=session).truncate_history("other", "run-0")

    assert (result.removed, result.error) == (0, "session not found")


async def test_a_failed_save_reports_the_error_and_keeps_the_latched_count(
    tmp_path: Path,
) -> None:
    db = await _stored(tmp_path, [_run(0), _run(1)])
    session = _session(main_team=_Agent(db, fail_save=True))
    session.context_budget.latch(500)

    result = await _controller(tmp_path, session=session).truncate_history(SESSION, "run-1")

    assert (result.removed, result.error) == (0, "disk full")
    assert await _run_ids(db) == ["run-0", "run-1"]
    assert session.context_budget.last_input_tokens == 500


async def test_a_session_that_cannot_be_read_is_an_error_result_not_an_exception(
    tmp_path: Path,
) -> None:
    session = _session(main_team=_team(error=SQLAlchemyError("db down")))
    session.context_budget.latch(500)

    result = await _controller(tmp_path, session=session).truncate_history(SESSION, "run-1")

    assert result.removed == 0
    assert result.error == "could not read the session: db down"
    assert session.context_budget.last_input_tokens == 500


# ── pending and interrupted rows ────────────────────────────────────


class _Store:
    def __init__(self, rows: list[object] | None = None, error: Exception | None = None) -> None:
        self._rows = rows or []
        self._error = error

    async def alist_pending(self, session_id: str) -> list[object]:
        if self._error:
            raise self._error
        return self._rows

    async def alist_interrupted(self, session_id: str) -> list[object]:
        if self._error:
            raise self._error
        return self._rows

    async def adiscard_interrupted(self, session_id: str, message_id: str) -> int:
        if self._error:
            raise self._error
        return 1


async def test_a_pending_message_is_shown_once_it_is_as_old_as_the_staleness_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    limit = now - PENDING_STALENESS_SECONDS
    rows = [
        PendingMessage("old", SESSION, "older", limit - 5),
        PendingMessage("edge", SESSION, "edge", limit),
        PendingMessage("fresh", SESSION, "fresh", limit + 1),
    ]

    shown = await _controller(tmp_path, pending_store=_Store(rows)).get_pending_messages(SESSION)

    assert [(p.message_id, p.content) for p in shown] == [("old", "older"), ("edge", "edge")]


async def test_a_pending_read_that_fails_shows_nothing(tmp_path: Path) -> None:
    store = _Store(error=SQLAlchemyError("db down"))

    assert await _controller(tmp_path, pending_store=store).get_pending_messages(SESSION) == []


async def test_an_interrupted_read_that_fails_shows_nothing(tmp_path: Path) -> None:
    store = _Store(error=SQLAlchemyError("db down"))

    assert await _controller(tmp_path, pending_store=store).get_interrupted_runs(SESSION) == []


async def test_a_discard_that_fails_in_storage_is_not_ok_and_says_why(tmp_path: Path) -> None:
    store = _Store(error=SQLAlchemyError("db down"))

    result = await _controller(tmp_path, pending_store=store).discard_interrupted_run(SESSION, "m")

    assert result.ok is False
    assert "db down" in result.error
