"""Agno's PostgreSQL storage, kept to one scope's rows.

``docs/CLOUD.md`` section 9. In the cloud an org's people and apps share
one database, and Agno's tables — sessions, memories, learnings — have
no org, producer or project column to filter by. What they do have is
``user_id``, on every row and as a filter on every read and delete, so
:class:`ScopedAsyncPostgresDb` puts the scope there
(:attr:`StorageScope.agno_user_id`, ``<org>/<producer>/<project>``):
each write is stamped with it and each read and delete is filtered by
it, whatever the caller passed. Like :class:`StorageScope` for igni's own
tables, it is the one place the filter lives.

A session is also stored under a key that carries the scope
(:class:`~igni_cloud.storage.scoped_session_ids.ScopedSessionIds`) and
handed back with the plain id. Agno's table and its columns are
unchanged — a wrapper, not a fork of its schema — so Agno's own
migrations still apply.

Agno's other tables — metrics, evals, knowledge, traces, spans,
schedules, approvals — are not scoped. igni does not write them.
"""

from __future__ import annotations

import copy
from typing import Any, ClassVar

from agno.db.base import SessionType
from agno.db.postgres import AsyncPostgresDb
from agno.db.schemas.memory import UserMemory
from agno.run.base import RunStatus
from agno.session import Session
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine

from igni_cloud.storage.agno_table_cache import AgnoTableCache, AgnoTableKey
from igni_cloud.storage.scoped_session_ids import AgnoRow, AgnoRun, ScopedSessionIds
from igni_core.core.db.scope import StorageScope


class ScopedAsyncPostgresDb(AsyncPostgresDb):
    """:class:`AsyncPostgresDb` that reads and writes one scope's rows."""

    #: Agno's name attribute for each kind of table igni uses.
    _NAME_ATTRIBUTES: ClassVar[dict[str, str]] = {
        "sessions": "session_table_name",
        "runs": "runs_table_name",
        "memories": "memory_table_name",
        "versions": "versions_table_name",
        "learnings": "learnings_table_name",
    }

    def __init__(
        self,
        *,
        scope: StorageScope,
        tables: AgnoTableCache,
        db_engine: AsyncEngine,
        session_table: str,
        memory_table: str,
        create_schema: bool = True,
    ) -> None:
        super().__init__(
            db_engine=db_engine,
            session_table=session_table,
            memory_table=memory_table,
            create_schema=create_schema,
        )
        self._scoped_user_id = scope.agno_user_id
        self._session_ids = ScopedSessionIds(self._scoped_user_id)
        self._tables = tables

    @property
    def scoped_user_id(self) -> str:
        return self._scoped_user_id

    def stored_id(self, session_id: str) -> str:
        """The key a session is stored under: the scope, then the id."""
        return self._session_ids.stored(session_id)

    # ── Tables ────────────────────────────────────────────────────

    async def _get_table(
        self, table_type: str, create_table_if_not_found: bool | None = False
    ) -> Table | None:
        """Agno's table, from :class:`AgnoTableCache` once found.

        Agno has no public hook for its table lookup, so this overrides
        the private one it calls before every query.
        """
        attribute = self._NAME_ATTRIBUTES.get(table_type)
        if attribute is None:
            return await super()._get_table(table_type, create_table_if_not_found)
        key = AgnoTableKey(
            database=self.db_engine.url.render_as_string(hide_password=True),
            schema=self.db_schema or "",
            table_type=table_type,
            name=str(getattr(self, attribute, "")),
        )
        known = self._tables.get(key)
        if known is not None:
            return known
        table = await super()._get_table(table_type, create_table_if_not_found)
        if table is not None:
            self._tables.remember(key, table)
        return table

    # ── Sessions ──────────────────────────────────────────────────

    async def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        return await super().delete_session(
            self.stored_id(session_id), user_id=self._scoped_user_id
        )

    async def delete_sessions(self, session_ids: list[str], user_id: str | None = None) -> None:
        await super().delete_sessions(
            [self.stored_id(s) for s in session_ids], user_id=self._scoped_user_id
        )

    async def get_session(
        self,
        session_id: str,
        session_type: SessionType | None = None,
        user_id: str | None = None,
        deserialize: bool | None = True,
        runs_limit: int | None = None,
    ) -> Session | AgnoRow | None:
        return self._session_ids.plain_found_session(
            await super().get_session(
                self.stored_id(session_id),
                session_type=session_type,
                user_id=self._scoped_user_id,
                deserialize=deserialize,
                runs_limit=runs_limit,
            )
        )

    async def get_sessions(
        self,
        session_type: SessionType | None = None,
        user_id: str | None = None,
        component_id: str | None = None,
        session_name: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
        include_runs: bool = True,
        filter_expr: dict[str, Any] | None = None,
    ) -> list[Session] | tuple[list[AgnoRow], int]:
        found = await super().get_sessions(
            session_type=session_type,
            user_id=self._scoped_user_id,
            component_id=component_id,
            session_name=session_name,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            limit=limit,
            page=page,
            sort_by=sort_by,
            sort_order=sort_order,
            deserialize=deserialize,
            include_runs=include_runs,
            filter_expr=filter_expr,
        )
        if isinstance(found, tuple):
            return self._session_ids.plain_rows_page(found)
        return [self._session_ids.plain_session(session) for session in found]

    async def rename_session(
        self,
        session_id: str,
        session_type: SessionType | None,
        session_name: str,
        user_id: str | None = None,
        deserialize: bool | None = True,
    ) -> Session | AgnoRow | None:
        return self._session_ids.plain_found_session(
            await super().rename_session(
                self.stored_id(session_id),
                session_type,
                session_name,
                user_id=self._scoped_user_id,
                deserialize=deserialize,
            )
        )

    async def upsert_session(
        self, session: Session, deserialize: bool | None = True
    ) -> Session | AgnoRow | None:
        # A copy: the caller's session keeps its plain id.
        stored = copy.copy(session)
        stored.session_id = self.stored_id(session.session_id)
        stored.user_id = self._scoped_user_id
        return self._session_ids.plain_found_session(
            await super().upsert_session(stored, deserialize=deserialize)
        )

    # ── Runs ──────────────────────────────────────────────────────
    #
    # Agno 3 keeps each run in a row of its own (``<session_table>_runs``),
    # keyed by ``run_id`` and tied to its session's row by ``session_id``.
    # A run is written under the session's stored id and the scope's
    # ``user_id`` — the column the row-level security policy reads — and
    # every read, cancel and delete is limited to the scope's runs.

    async def upsert_run(
        self,
        run: AgnoRun | AgnoRow,
        session_id: str,
        user_id: str | None = None,
        run_index: int | None = None,
    ) -> None:
        await super().upsert_run(
            run,
            session_id=self.stored_id(session_id),
            user_id=self._scoped_user_id,
            run_index=run_index,
        )

    async def get_runs(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        workflow_id: str | None = None,
        status: RunStatus | None = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
    ) -> list[AgnoRun] | tuple[list[AgnoRow], int]:
        found = await super().get_runs(
            session_id=self.stored_id(session_id) if session_id is not None else None,
            user_id=self._scoped_user_id,
            agent_id=agent_id,
            team_id=team_id,
            workflow_id=workflow_id,
            status=status,
            limit=limit,
            page=page,
            sort_by=sort_by,
            sort_order=sort_order,
            deserialize=deserialize,
        )
        if isinstance(found, tuple):
            return self._session_ids.plain_rows_page(found)
        return [self._session_ids.plain_run(run) for run in found]

    async def get_run(
        self, run_id: str, deserialize: bool | None = True
    ) -> AgnoRun | AgnoRow | None:
        if not await self._owned([run_id]):
            return None
        return self._session_ids.plain_found_run(
            await super().get_run(run_id, deserialize=deserialize)
        )

    async def cancel_pending_run(
        self,
        run_id: str,
        *,
        session_id: str,
        reason: str,
        user_id: str | None = None,
        expected_status: str = "PENDING",
    ) -> bool:
        return await super().cancel_pending_run(
            run_id,
            session_id=self.stored_id(session_id),
            reason=reason,
            user_id=self._scoped_user_id,
            expected_status=expected_status,
        )

    async def delete_run(self, run_id: str) -> bool:
        if not await self._owned([run_id]):
            return False
        return await super().delete_run(run_id)

    async def delete_runs(self, run_ids: list[str]) -> None:
        owned = await self._owned(run_ids)
        if owned:
            await super().delete_runs(owned)

    async def _owned(self, run_ids: list[str]) -> list[str]:
        """Which of ``run_ids`` are this scope's: Agno's own run-id calls take no scope."""
        if not run_ids:
            return []
        table = await self._get_table(table_type="runs")
        if table is None:
            return []
        async with self.async_session_factory() as sess:
            found = await sess.execute(
                select(table.c.run_id).where(
                    table.c.run_id.in_(run_ids), table.c.user_id == self._scoped_user_id
                )
            )
            return [row[0] for row in found]

    # ── Memories ──────────────────────────────────────────────────

    async def delete_user_memory(self, memory_id: str, user_id: str | None = None) -> None:
        await super().delete_user_memory(memory_id, user_id=self._scoped_user_id)

    async def delete_user_memories(self, memory_ids: list[str], user_id: str | None = None) -> None:
        await super().delete_user_memories(memory_ids, user_id=self._scoped_user_id)

    async def get_all_memory_topics(self, user_id: str | None = None) -> list[str]:
        return await super().get_all_memory_topics(user_id=self._scoped_user_id)

    async def get_user_memory(
        self,
        memory_id: str,
        deserialize: bool | None = True,
        user_id: str | None = None,
    ) -> UserMemory | AgnoRow | None:
        return await super().get_user_memory(
            memory_id, deserialize=deserialize, user_id=self._scoped_user_id
        )

    async def get_user_memories(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        topics: list[str] | None = None,
        search_content: str | None = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
    ) -> list[UserMemory] | tuple[list[AgnoRow], int]:
        return await super().get_user_memories(
            user_id=self._scoped_user_id,
            agent_id=agent_id,
            team_id=team_id,
            topics=topics,
            search_content=search_content,
            limit=limit,
            page=page,
            sort_by=sort_by,
            sort_order=sort_order,
            deserialize=deserialize,
        )

    async def get_user_memory_stats(
        self, limit: int | None = None, page: int | None = None, user_id: str | None = None
    ) -> tuple[list[AgnoRow], int]:
        return await super().get_user_memory_stats(
            limit=limit, page=page, user_id=self._scoped_user_id
        )

    async def upsert_user_memory(
        self, memory: UserMemory, deserialize: bool | None = True
    ) -> UserMemory | AgnoRow | None:
        memory.user_id = self._scoped_user_id
        return await super().upsert_user_memory(memory, deserialize=deserialize)

    async def clear_memories(self) -> None:
        """Only this scope's: Agno's clears the whole table."""
        memories = await super().get_user_memories(user_id=self._scoped_user_id)
        ids = [m.memory_id for m in memories if isinstance(m, UserMemory) and m.memory_id]
        if ids:
            await super().delete_user_memories(ids, user_id=self._scoped_user_id)

    # ── Learnings ─────────────────────────────────────────────────

    async def get_learning(
        self,
        learning_type: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        session_id: str | None = None,
        namespace: str | None = None,
        entity_id: str | None = None,
        entity_type: str | None = None,
    ) -> AgnoRow | None:
        return await super().get_learning(
            learning_type,
            user_id=self._scoped_user_id,
            agent_id=agent_id,
            team_id=team_id,
            session_id=session_id,
            namespace=namespace,
            entity_id=entity_id,
            entity_type=entity_type,
        )

    async def upsert_learning(
        self,
        id: str,
        learning_type: str,
        content: dict[str, Any],
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        session_id: str | None = None,
        namespace: str | None = None,
        entity_id: str | None = None,
        entity_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await super().upsert_learning(
            id,
            learning_type,
            content,
            user_id=self._scoped_user_id,
            agent_id=agent_id,
            team_id=team_id,
            session_id=session_id,
            namespace=namespace,
            entity_id=entity_id,
            entity_type=entity_type,
            metadata=metadata,
        )

    async def get_learnings(
        self,
        learning_type: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        session_id: str | None = None,
        namespace: str | None = None,
        entity_id: str | None = None,
        entity_type: str | None = None,
        limit: int | None = None,
    ) -> list[AgnoRow]:
        return await super().get_learnings(
            learning_type,
            user_id=self._scoped_user_id,
            agent_id=agent_id,
            team_id=team_id,
            session_id=session_id,
            namespace=namespace,
            entity_id=entity_id,
            entity_type=entity_type,
            limit=limit,
        )

    async def list_learnings(
        self,
        learning_type: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        session_id: str | None = None,
        namespace: str | None = None,
        entity_id: str | None = None,
        entity_type: str | None = None,
        include_global: bool = False,
        limit: int = 100,
        page: int = 1,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> tuple[list[AgnoRow], int]:
        """This scope's only: ``include_global`` is never honoured."""
        return await super().list_learnings(
            learning_type,
            user_id=self._scoped_user_id,
            agent_id=agent_id,
            team_id=team_id,
            session_id=session_id,
            namespace=namespace,
            entity_id=entity_id,
            entity_type=entity_type,
            include_global=False,
            limit=limit,
            page=page,
            sort_by=sort_by,
            sort_order=sort_order,
        )


__all__ = ["ScopedAsyncPostgresDb"]
