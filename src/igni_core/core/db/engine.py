"""The process's one :class:`~igni_core.core.db.engine_registry.EngineRegistry`.

A database must have exactly one SQLAlchemy pool per process: two pools
on one SQLite file contend for its lock, and two on one PostgreSQL
database double the connections a producer's login holds. That
invariant is process-wide by nature, so this module holds the one
registry every :class:`~igni_core.core.db.database.Database` handle and
the schema check draw their engines and sessionmakers from.

Tests that need isolation construct their own ``EngineRegistry()`` and
use it directly, never on a database this one is also serving.
"""

from __future__ import annotations

from igni_core.core.db.engine_registry import EngineRegistry

__all__ = ["engines"]

#: The engines and sessionmakers of every database this process opens.
engines = EngineRegistry()
