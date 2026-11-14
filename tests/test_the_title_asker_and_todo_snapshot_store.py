"""The title asker and the todo snapshot store, each on its own.

The naming test drives the asker through a real session; this file pins
its pure rules and its edges directly, and the todo store's key, its
camelCase dump, its row filtering and its failure envelopes against
Agno's real SQLite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agno.db.base import SessionType
from agno.db.sqlite import AsyncSqliteDb

from igni_core.core.session.persistence import SessionDataService
from igni_core.core.session.persistence.title_asker import TitleAsker
from igni_core.core.session.persistence.todos_store import TodoSnapshotStore
from igni_core.core.tools.todo import TodoItemWire

SESSION = "sess-todos"


class _Answer:
    def __init__(self, content: Any) -> None:
        self.content = content


class _Model:
    """Answers from a script; an Exception in the script is raised."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.asked: list[list[Any]] = []
        self.max_tokens: int | None = None

    async def aresponse(self, *, messages: list[Any]) -> Any:
        self.asked.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Answer(answer)


class _NoMaxTokens:
    """A model that refuses the token budget."""

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(name)


# ── TitleAsker: the opening ──────────────────────────────────────────


class TestOpening:
    def test_system_context_and_extension_tags_are_dropped_and_texts_joined(self) -> None:
        texts = [
            "<system-context>now is noon</system-context>\nFind the port",
            '<extension-message source="x">Then restart it</extension-message>',
        ]
        assert TitleAsker.opening(texts) == "Find the port\n\nThen restart it"

    def test_empty_parts_are_dropped(self) -> None:
        assert TitleAsker.opening(["<system-context>x</system-context>", "  ", ""]) == ""
        assert TitleAsker.opening([]) == ""

    def test_the_opening_is_capped_at_600_characters(self) -> None:
        assert len(TitleAsker.opening(["a" * 900])) == 600


# ── TitleAsker: the fallback ─────────────────────────────────────────


class TestFallback:
    def test_exactly_six_words_has_no_ellipsis(self) -> None:
        assert TitleAsker.fallback("one two three four five six") == "one two three four five six"

    def test_more_than_six_words_is_cut_with_an_ellipsis(self) -> None:
        assert TitleAsker.fallback("one two three four five six seven") == (
            "one two three four five six…"
        )

    def test_whitespace_collapses_and_trailing_punctuation_goes(self) -> None:
        assert TitleAsker.fallback("fix   the\n bug, now!") == "fix the bug, now"

    def test_over_50_characters_cuts_at_a_word_boundary(self) -> None:
        opening = "internationalization configuration documentation reorganization plan"
        assert TitleAsker.fallback(opening) == "internationalization configuration documentation…"

    def test_a_single_overlong_word_is_hard_cut(self) -> None:
        assert TitleAsker.fallback("x" * 80) == "x" * 50 + "…"

    def test_an_empty_opening_is_an_empty_title(self) -> None:
        assert TitleAsker.fallback("") == ""
        assert TitleAsker.fallback("   ") == ""


# ── TitleAsker: what counts as a title ───────────────────────────────


class TestUsable:
    def test_more_than_six_words_is_not_a_title(self) -> None:
        assert TitleAsker.usable("one two three four five six seven") == ""
        assert TitleAsker.usable("one two three four five six") == "one two three four five six"

    def test_more_than_sixty_characters_is_not_a_title(self) -> None:
        assert TitleAsker.usable("a" * 61) == ""
        assert TitleAsker.usable("a" * 60) == "a" * 60

    def test_an_unterminated_think_swallows_the_answer(self) -> None:
        assert TitleAsker.usable("<think>maybe Port lookup") == ""

    def test_a_closed_think_is_removed_and_dots_trimmed(self) -> None:
        assert TitleAsker.usable("<think>hmm</think>\nPort lookup...") == "Port lookup"

    def test_none_and_empty_are_not_titles(self) -> None:
        assert TitleAsker.usable(None) == ""  # type: ignore[arg-type]
        assert TitleAsker.usable("") == ""

    def test_two_lines_are_not_a_title(self) -> None:
        assert TitleAsker.usable("Port lookup\nSecond line") == ""


# ── TitleAsker: asking ───────────────────────────────────────────────


class TestAsk:
    async def test_an_empty_opening_asks_nothing(self) -> None:
        model = _Model(["Port lookup"])
        assert await TitleAsker(model).ask("") == ""
        assert model.asked == []

    async def test_no_model_goes_straight_to_the_fallback(self) -> None:
        assert await TitleAsker(None).ask("find the port please") == "find the port please"

    async def test_the_retry_uses_the_retry_instruction_and_its_title_wins(self) -> None:
        model = _Model(["1. a\n2. b", "Port lookup"])
        assert await TitleAsker(model).ask("find the port") == "Port lookup"
        system = [m[0].content for m in model.asked]
        assert system == [TitleAsker._INSTRUCTION, TitleAsker._RETRY_INSTRUCTION]
        assert model.asked[0][1].content == "Request:\nfind the port\n\nTitle:"

    async def test_two_unusable_answers_fall_back_to_the_opening(self) -> None:
        model = _Model(["", "a b c d e f g"])
        assert await TitleAsker(model).ask("find the port") == "find the port"
        assert len(model.asked) == 2

    async def test_a_raising_model_is_swallowed_and_the_opening_is_used(self) -> None:
        model = _Model([RuntimeError("net"), RuntimeError("net")])
        assert await TitleAsker(model).ask("find the port") == "find the port"

    async def test_non_string_content_is_not_a_title(self) -> None:
        model = _Model([None, ["Port lookup"]])
        assert await TitleAsker(model).ask("find the port") == "find the port"

    async def test_the_live_model_is_not_bounded_only_its_copy(self) -> None:
        model = _Model(["Port lookup"])
        asker = TitleAsker(model)
        assert model.max_tokens is None
        assert asker._model.max_tokens == 256
        assert asker._model is not model

    async def test_a_model_that_refuses_the_budget_is_still_used_unbounded(self) -> None:
        asker = TitleAsker(_NoMaxTokens())
        assert asker._model is not None


# ── TodoSnapshotStore ────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> AsyncSqliteDb:
    return AsyncSqliteDb(
        db_file=str(tmp_path / "state.db"),
        session_table="igni_sessions",
        memory_table="igni_memories",
    )


class _Flaky:
    def __init__(self, real: AsyncSqliteDb, *failing: str) -> None:
        self._real = real
        self._failing = set(failing)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if name not in self._failing:
            return attr

        async def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError(f"{name} is down")

        return boom


async def _data(db: Any) -> dict[str, Any]:
    stored = await db.get_session(session_id=SESSION, session_type=SessionType.AGENT)
    return stored.session_data


def _todo(content: str, status: str = "pending", form: str = "Doing it") -> TodoItemWire:
    return TodoItemWire(content=content, status=status, active_form=form)


class TestTodoSnapshotStore:
    def test_the_key_is_todos(self) -> None:
        assert TodoSnapshotStore.KEY == "todos"

    async def test_rows_persist_with_the_camel_case_key_and_load_in_order(
        self, db: AsyncSqliteDb
    ) -> None:
        store = TodoSnapshotStore(SessionDataService(db, SESSION))
        assert (await store.save([_todo("b", "in_progress"), _todo("a")])).ok
        rows = (await _data(db))["todos"]
        assert [r["content"] for r in rows] == ["b", "a"]
        assert all("activeForm" in r and "active_form" not in r for r in rows)
        loaded = await store.load()
        assert [t.content for t in loaded.value] == ["b", "a"]
        assert loaded.value[0].status == "in_progress"

    async def test_malformed_rows_drop_on_load_and_a_non_list_loads_empty(
        self, db: AsyncSqliteDb
    ) -> None:
        service = SessionDataService(db, SESSION)
        store = TodoSnapshotStore(service)
        await service.write_key(
            "todos",
            [
                {"content": "kept", "status": "pending", "activeForm": "Keeping"},
                {"content": "", "status": "pending"},
                {"content": "bad", "status": "exploded"},
            ],
        )
        assert [t.content for t in (await store.load()).value] == ["kept"]
        await service.write_key("todos", {"content": "x"})
        assert (await store.load()).value == []

    async def test_an_empty_save_clears_the_snapshot_and_keeps_siblings(
        self, db: AsyncSqliteDb
    ) -> None:
        service = SessionDataService(db, SESSION)
        store = TodoSnapshotStore(service)
        await service.write_key("plan", {"keep": "me"})
        await store.save([_todo("a")])
        assert (await store.save([])).ok
        data = await _data(db)
        assert data["todos"] == []
        assert data["plan"] == {"keep": "me"}
        assert (await store.load()).value == []

    async def test_a_failed_write_is_reported_and_a_failed_read_does_not_raise(
        self, db: AsyncSqliteDb
    ) -> None:
        down = TodoSnapshotStore(SessionDataService(_Flaky(db, "upsert_session"), SESSION))
        result = await down.save([_todo("a")])
        assert result.ok is False
        assert result.error == "upsert_session is down"
        unreadable = TodoSnapshotStore(SessionDataService(_Flaky(db, "get_session"), SESSION))
        loaded = await unreadable.load()
        assert loaded.ok is False
        assert loaded.error == "get_session is down"

    async def test_no_database_is_a_no_op_and_loads_nothing(self) -> None:
        store = TodoSnapshotStore(SessionDataService(None, SESSION))
        assert (await store.save([_todo("a")])).ok
        loaded = await store.load()
        assert loaded.ok
        assert loaded.value is None
