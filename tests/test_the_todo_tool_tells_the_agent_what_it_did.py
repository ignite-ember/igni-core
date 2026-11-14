"""What ``todo_write`` tells the agent and the clients, and how the
todo rows are read at the edges.

The row rules, the reply text, the broadcast and the persisted snapshot
are all things the agent or an SDK client sees; each is read here
through the toolkit and the typed rows, not through their internals.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.core.session.schemas import PersistResult
from igni_core.core.tools.todo import (
    TodoBatch,
    TodoItemInput,
    TodoItemWire,
    TodoStore,
    TodosUpdatedEvent,
    TodoTools,
)


class _Persistence:
    def __init__(self, result: PersistResult | None = None) -> None:
        self.saved: list[list[TodoItemWire]] = []
        self._result = result or PersistResult(ok=True)

    async def save_todos(self, todos: list[TodoItemWire]) -> PersistResult:
        self.saved.append(list(todos))
        return self._result


def _session(result: PersistResult | None = None):
    session = SimpleNamespace(
        todo_store=TodoStore(),
        persistence=_Persistence(result),
        broadcast_log=[],
    )
    session.broadcast = session.broadcast_log.append
    return session


class TestValidateBatchEdges:
    def test_nothing_is_an_empty_batch(self):
        assert TodoItemInput.validate_batch(None) == TodoBatch()

    @pytest.mark.parametrize("rows", ["abc", {"a": 1}, 5])
    def test_anything_but_a_list_is_one_error(self, rows):
        batch = TodoItemInput.validate_batch(rows)

        assert batch.items == []
        assert batch.errors == ["todos must be a list"]

    def test_a_tuple_of_rows_is_accepted(self):
        batch = TodoItemInput.validate_batch(({"content": "a"}, {"content": "b"}))

        assert [i.content for i in batch.items] == ["a", "b"]

    @pytest.mark.parametrize("row", ["text", 3, None])
    def test_a_row_that_is_not_an_object_is_named_by_its_index(self, row):
        batch = TodoItemInput.validate_batch([{"content": "ok"}, row])

        assert [i.content for i in batch.items] == ["ok"]
        assert batch.errors == ["todos[1]: not a dict"]

    def test_a_status_the_agent_spelled_out_empty_is_refused(self):
        batch = TodoItemInput.validate_batch([{"content": "a", "status": ""}])

        assert batch.items == []
        assert batch.errors == [
            "todos[0]: status '' not in ['completed', 'in_progress', 'pending']"
        ]

    def test_a_null_status_counts_as_spelled_out_empty(self):
        batch = TodoItemInput.validate_batch([{"content": "a", "status": None}])

        assert batch.errors == [
            "todos[0]: status '' not in ['completed', 'in_progress', 'pending']"
        ]

    def test_the_unknown_status_is_shown_lowercased(self):
        batch = TodoItemInput.validate_batch([{"content": "a", "status": " Done "}])

        assert batch.errors == [
            "todos[0]: status 'done' not in ['completed', 'in_progress', 'pending']"
        ]

    def test_numbers_become_their_text_and_values_are_stripped(self):
        batch = TodoItemInput.validate_batch(
            [{"content": 42, "status": " Completed ", "activeForm": "  Doing  "}]
        )

        assert batch.errors == []
        assert batch.items == [TodoItemWire(content="42", status="completed", active_form="Doing")]

    def test_a_null_content_is_empty_content(self):
        batch = TodoItemInput.validate_batch([{"content": None, "status": "pending"}])

        assert batch.errors == ["todos[0]: empty content"]

    def test_unicode_content_is_kept(self):
        batch = TodoItemInput.validate_batch([{"content": "Перевірити тести ✓"}])

        assert batch.items[0].content == "Перевірити тести ✓"

    def test_good_and_bad_rows_in_one_batch_each_get_their_own_verdict(self):
        batch = TodoItemInput.validate_batch(
            [{"content": "a"}, {"content": ""}, {"content": "c", "status": "x"}, {"content": "d"}]
        )

        assert [i.content for i in batch.items] == ["a", "d"]
        assert [e.split(":")[0] for e in batch.errors] == ["todos[1]", "todos[2]"]


class TestSnapshotsOnDisk:
    def test_a_snapshot_that_is_not_a_list_reads_as_empty(self):
        assert TodoItemWire.parse_snapshot({"content": "a"}) == []
        assert TodoItemWire.parse_snapshot(None) == []

    def test_corrupt_rows_drop_and_the_valid_ones_come_back(self):
        rows = TodoItemWire.parse_snapshot(
            [
                {"content": "keep", "status": "in_progress", "activeForm": "Keeping"},
                "junk",
                {"content": "", "status": "pending"},
                {"content": "bad status", "status": "nope"},
                {"content": "also keep", "status": "completed"},
            ]
        )

        assert rows == [
            TodoItemWire(content="keep", status="in_progress", active_form="Keeping"),
            TodoItemWire(content="also keep", status="completed"),
        ]


class TestWhatTheClientsSee:
    def test_the_event_is_on_the_todos_updated_channel_with_camel_case_rows(self):
        event = TodosUpdatedEvent(
            todos=[TodoItemWire(content="Run tests", status="in_progress", active_form="Running")]
        )

        dumped = event.model_dump()

        assert dumped["channel"] == "todos_updated"
        assert dumped["todos"] == [
            {"content": "Run tests", "status": "in_progress", "activeForm": "Running"}
        ]

    def test_every_published_field_has_a_description_for_the_sdks(self):
        for model in (TodoItemWire, TodosUpdatedEvent):
            fields = model.model_json_schema(by_alias=True)["properties"]
            for name in (
                ("content", "status", "activeForm") if model is TodoItemWire else ("todos",)
            ):
                assert fields[name].get("description"), f"{model.__name__}.{name}"

    def test_the_tools_argument_rows_are_described_for_the_sdks(self):
        fields = TodoItemInput.model_json_schema(by_alias=True)["properties"]

        assert all(fields[name].get("description") for name in ("content", "status", "activeForm"))


class TestTodoWriteReplies:
    async def test_the_toolkit_registers_only_todo_write(self):
        tools = TodoTools(_session())

        assert set(tools.functions) | set(tools.async_functions) == {"todo_write"}

    async def test_the_reply_counts_each_status_in_one_sentence(self):
        session = _session()

        reply = await TodoTools(session).todo_write(
            [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "in_progress"},
                {"content": "c"},
            ]
        )

        assert reply == "3 todos: 1 completed, 1 in_progress, 1 pending."

    async def test_the_list_is_broadcast_whole_and_persisted_after_it_is_set(self):
        session = _session()

        await TodoTools(session).todo_write(
            [{"content": "a"}, {"content": "b", "status": "completed"}]
        )

        rows = [
            TodoItemWire(content="a", status="pending"),
            TodoItemWire(content="b", status="completed"),
        ]
        assert session.broadcast_log == [TodosUpdatedEvent(todos=rows)]
        assert session.persistence.saved == [rows]

    async def test_validation_errors_are_appended_after_the_counts(self):
        reply = await TodoTools(_session()).todo_write([{"content": "a"}, {"content": ""}, "x"])

        assert reply == (
            "1 todos: 0 completed, 0 in_progress, 1 pending."
            " Validation errors (ignored): todos[1]: empty content; todos[2]: not a dict"
        )

    async def test_two_in_progress_rows_get_the_one_at_a_time_note(self):
        reply = await TodoTools(_session()).todo_write(
            [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]
        )

        assert reply.startswith("2 todos: 0 completed, 2 in_progress, 0 pending.")
        assert reply.endswith(
            " Note: keep at most one item in_progress at a time —"
            " mark the previous in_progress item completed before"
            " starting another."
        )

    async def test_an_empty_list_clears_broadcasts_and_persists(self):
        session = _session()
        session.todo_store.set([TodoItemWire(content="old", status="pending")])

        reply = await TodoTools(session).todo_write([])

        assert reply == "Cleared todo list."
        assert session.todo_store.items == []
        assert session.broadcast_log == [TodosUpdatedEvent(todos=[])]
        assert session.persistence.saved == [[]]

    async def test_only_invalid_rows_leave_the_list_alone_and_say_nothing_on_the_wire(self):
        session = _session()
        old = [TodoItemWire(content="old", status="pending")]
        session.todo_store.set(old)

        reply = await TodoTools(session).todo_write([{"content": ""}, 7])

        assert reply == (
            "0 todos: 0 completed, 0 in_progress, 0 pending."
            " Validation errors (ignored): todos[0]: empty content; todos[1]: not a dict"
        )
        assert session.todo_store.items == old
        assert session.broadcast_log == []
        assert session.persistence.saved == []

    async def test_a_todos_value_that_is_not_a_list_changes_nothing(self):
        session = _session()

        reply = await TodoTools(session).todo_write("do things")  # type: ignore[arg-type]

        assert reply.endswith("Validation errors (ignored): todos must be a list")
        assert session.broadcast_log == []
        assert session.persistence.saved == []


class TestAFailedSnapshotIsReported:
    async def test_the_agent_is_told_the_snapshot_did_not_land(self):
        session = _session(PersistResult(ok=False, error="disk full"))

        reply = await TodoTools(session).todo_write([{"content": "a"}])

        assert reply == (
            "1 todos: 0 completed, 0 in_progress, 1 pending."
            " Warning: the todo snapshot was not persisted (disk full)."
        )
        assert session.todo_store.items == [TodoItemWire(content="a", status="pending")]
        assert session.broadcast_log != []

    async def test_clearing_reports_a_failed_snapshot_too(self):
        session = _session(PersistResult(ok=False, error="disk full"))

        reply = await TodoTools(session).todo_write([])

        assert (
            reply == "Cleared todo list. Warning: the todo snapshot was not persisted (disk full)."
        )

    async def test_a_batch_with_only_errors_has_no_snapshot_to_warn_about(self):
        session = _session(PersistResult(ok=False, error="disk full"))

        reply = await TodoTools(session).todo_write([{"content": ""}])

        assert "Warning" not in reply
