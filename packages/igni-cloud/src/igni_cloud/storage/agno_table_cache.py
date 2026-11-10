"""Agno's tables, once found, kept for the life of the storage that found them.

Agno checks that a table exists, that its columns are right and reflects
it on *every* call — three round trips before each query. The schema is
fixed while igni runs (``igni migrate`` made it, as the admin role), so
a table once found is kept here. One not found is not kept: it may be
made later.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from sqlalchemy import Table


@dataclass(frozen=True, slots=True)
class AgnoTableKey:
    """Which table: the database (password hidden), schema, Agno's kind and name."""

    database: str
    schema: str
    table_type: str
    name: str


class AgnoTableCache:
    """Tables found, per :class:`AgnoTableKey`; safe to share across threads."""

    def __init__(self) -> None:
        self._tables: dict[AgnoTableKey, Table] = {}
        self._lock = threading.Lock()

    def get(self, key: AgnoTableKey) -> Table | None:
        with self._lock:
            return self._tables.get(key)

    def remember(self, key: AgnoTableKey, table: Table) -> None:
        with self._lock:
            self._tables[key] = table


__all__ = ["AgnoTableCache", "AgnoTableKey"]
