"""Tests for ``TodoWrite`` — CC's planning tool, ember parity.

Covers:
- ``TodoStore`` set/items
- ``TodoItemInput.validate_batch`` validation (status enum, required
  content, camelCase ↔ snake_case ``activeForm`` aliasing)
- ``TodoTools.todo_write`` semantics (atomic replace, summary
  string, multi-in_progress warning, validation error surfacing)
- The ``GET_TODOS`` RPC endpoint
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.core.session.schemas import PersistResult
from igni_core.core.tools.todo import (
    TodoItemInput,
    TodoItemWire,
    TodoStore,
    TodoTools,
)
from igni_core.protocol.rpc import RpcMethod
from tests.backend_server_factory import make_backend
from tests.rpc_tables import rpc_table_for

# ── Pure data layer ───────────────────────────────────────────


class TestTodoStore:
    def test_set_replaces_atomically(self):
        store = TodoStore()
        store.set(
            [
                TodoItemWire(content="a", status="pending"),
                TodoItemWire(content="b", status="completed"),
            ]
        )
        store.set([TodoItemWire(content="c", status="in_progress", active_form="Doing c")])
        # Old items are gone — no merge.
        assert len(store.items) == 1
        assert store.items[0].content == "c"

    def test_rows_dump_camelcase_active_form(self):
        store = TodoStore()
        store.set(
            [TodoItemWire(content="Run tests", status="in_progress", active_form="Running tests")]
        )
        assert [row.model_dump() for row in store.items] == [
            {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"}
        ]

    def test_empty_store_has_no_rows(self):
        assert TodoStore().items == []


# ── Validation ────────────────────────────────────────────────


class TestValidateBatch:
    def test_valid_entries_pass(self):
        batch = TodoItemInput.validate_batch(
            [
                {"content": "a", "status": "pending"},
                {"content": "b", "status": "in_progress", "activeForm": "Doing b"},
                {"content": "c", "status": "completed"},
            ]
        )
        assert batch.errors == []
        assert [i.content for i in batch.items] == ["a", "b", "c"]
        assert batch.items[1].active_form == "Doing b"

    def test_non_list_input(self):
        batch = TodoItemInput.validate_batch({"not": "a list"})
        assert batch.items == []
        assert any("list" in e for e in batch.errors)

    def test_empty_content_rejected(self):
        batch = TodoItemInput.validate_batch([{"content": "  ", "status": "pending"}])
        assert batch.items == []
        assert any("empty content" in e for e in batch.errors)

    def test_unknown_status_rejected(self):
        batch = TodoItemInput.validate_batch([{"content": "a", "status": "wat"}])
        assert batch.items == []
        assert any("status" in e and "wat" in e for e in batch.errors)

    def test_non_dict_row_rejected(self):
        batch = TodoItemInput.validate_batch(["not a dict", {"content": "a", "status": "pending"}])
        # Bad row dropped, good row kept.
        assert [i.content for i in batch.items] == ["a"]
        assert any("not a dict" in e for e in batch.errors)

    def test_snake_case_active_form_accepted(self):
        """The agent might output either ``activeForm`` (CC
        convention) or ``active_form`` (Python-natural). Accept
        both so the model doesn't trip on the casing."""
        batch = TodoItemInput.validate_batch(
            [{"content": "a", "status": "in_progress", "active_form": "Doing a"}]
        )
        assert batch.items[0].active_form == "Doing a"

    def test_status_is_normalized_lowercase(self):
        batch = TodoItemInput.validate_batch([{"content": "a", "status": "IN_PROGRESS"}])
        assert batch.errors == []
        assert batch.items[0].status == "in_progress"

    def test_absent_status_defaults_to_pending(self):
        batch = TodoItemInput.validate_batch([{"content": "a"}])
        assert batch.errors == []
        assert batch.items[0].status == "pending"

    def test_rows_already_validated_are_accepted(self):
        """The tool boundary hands the validator typed rows — the
        same rules must not fire twice."""
        batch = TodoItemInput.validate_batch([TodoItemInput(content="a", status="in_progress")])
        assert batch.errors == []
        assert batch.items[0].status == "in_progress"


# ── Tool behavior ─────────────────────────────────────────────


def _make_session():
    session = MagicMock()
    session.todo_store = TodoStore()
    # ``todo_write`` flushes the snapshot to the persistence layer;
    # the stub answers with the Pattern-3 envelope the real layer
    # returns.
    session.persistence = SimpleNamespace(save_todos=AsyncMock(return_value=PersistResult(ok=True)))
    return session


class TestTodoWrite:
    @pytest.mark.asyncio
    async def test_replaces_list_on_each_call(self):
        session = _make_session()
        tool = TodoTools(session)
        await tool.todo_write([{"content": "a", "status": "pending"}])
        await tool.todo_write([{"content": "b", "status": "in_progress"}])
        assert [i.content for i in session.todo_store.items] == ["b"]

    @pytest.mark.asyncio
    async def test_summary_counts_each_status(self):
        session = _make_session()
        tool = TodoTools(session)
        result = await tool.todo_write(
            [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "in_progress"},
                {"content": "c", "status": "pending"},
                {"content": "d", "status": "pending"},
            ]
        )
        assert "4 todos" in result
        assert "1 completed" in result
        assert "1 in_progress" in result
        assert "2 pending" in result

    @pytest.mark.asyncio
    async def test_empty_list_clears(self):
        session = _make_session()
        session.todo_store.set([TodoItemWire(content="old", status="pending")])
        tool = TodoTools(session)
        result = await tool.todo_write([])
        assert "Cleared" in result
        assert session.todo_store.items == []

    @pytest.mark.asyncio
    async def test_multi_in_progress_warns_in_summary(self):
        """The "at most one in_progress" rule is encouraged via
        the reply string — the model sees the warning and self-
        corrects on the next call."""
        session = _make_session()
        tool = TodoTools(session)
        result = await tool.todo_write(
            [
                {"content": "a", "status": "in_progress"},
                {"content": "b", "status": "in_progress"},
            ]
        )
        assert "at most one" in result.lower()

    @pytest.mark.asyncio
    async def test_validation_errors_surfaced(self):
        """Bad rows are dropped (so the rest of the list still
        applies) and the errors come back in the reply so the
        agent can correct."""
        session = _make_session()
        tool = TodoTools(session)
        result = await tool.todo_write(
            [
                {"content": "a", "status": "pending"},
                {"content": "", "status": "pending"},  # bad: empty content
            ]
        )
        assert "Validation errors" in result
        assert "empty content" in result
        # Good row still applied.
        assert len(session.todo_store.items) == 1


# ── RPC ───────────────────────────────────────────────────────


class TestGetTodosRpc:
    def test_returns_snapshot(self, tmp_path):
        session = MagicMock()
        session.todo_store = TodoStore()
        session.todo_store.set(
            [TodoItemWire(content="Run tests", status="in_progress", active_form="Running tests")]
        )
        backend = make_backend(session)
        out = backend.get_todos()
        assert out == [
            TodoItemWire(content="Run tests", status="in_progress", active_form="Running tests")
        ]
        assert out[0].model_dump() == {
            "content": "Run tests",
            "status": "in_progress",
            "activeForm": "Running tests",
        }

    def test_returns_empty_when_no_list(self):
        """If the agent never called ``todo_write``, the store
        exists but is empty — RPC returns ``[]`` cleanly."""
        session = MagicMock()
        session.todo_store = TodoStore()
        backend = make_backend(session)
        assert backend.get_todos() == []

    @pytest.mark.asyncio
    async def test_dispatch_table_routes_get_todos(self):
        session = MagicMock()
        session.todo_store = TodoStore()
        session.todo_store.set([TodoItemWire(content="planned", status="pending")])
        backend = make_backend(session)

        table = rpc_table_for(backend, transport=MagicMock())
        handler = table.get(RpcMethod.GET_TODOS)
        assert handler is not None
        result = handler({})
        assert isinstance(result, list)
        assert result[0].content == "planned"


# ── Integration ─────────────────────────────────────────────


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_agent_loop_visible_via_rpc(self):
        """Simulate the realistic flow: agent calls todo_write
        twice (initial plan, then progresses one item), and the
        RPC at each step reflects exactly what was last written."""
        session = MagicMock()
        session.todo_store = TodoStore()
        session.persistence = SimpleNamespace(
            save_todos=AsyncMock(return_value=PersistResult(ok=True))
        )
        tool = TodoTools(session)
        backend = make_backend(session)

        # First call: plan three items.
        await tool.todo_write(
            [
                {"content": "Read spec", "status": "in_progress", "activeForm": "Reading spec"},
                {"content": "Write code", "status": "pending"},
                {"content": "Run tests", "status": "pending"},
            ]
        )
        snap = backend.get_todos()
        statuses = [e.status for e in snap]
        assert statuses == ["in_progress", "pending", "pending"]

        # Second call: mark step 1 done, step 2 in progress.
        await tool.todo_write(
            [
                {"content": "Read spec", "status": "completed"},
                {"content": "Write code", "status": "in_progress", "activeForm": "Writing code"},
                {"content": "Run tests", "status": "pending"},
            ]
        )
        snap = backend.get_todos()
        statuses = [e.status for e in snap]
        assert statuses == ["completed", "in_progress", "pending"]
        # Active form for the active step is what the UI renders.
        assert snap[1].active_form == "Writing code"
