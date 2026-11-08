"""Shared database layer — SQLAlchemy + alembic, SQLite or PostgreSQL.

This module is the single import point for the DB stack:

- :class:`Base` from :mod:`igni_core.core.db.base` is the declarative base
  every ORM model registers against.
- :class:`igni_core.core.db.urls.DatabaseUrl` names a database: a SQLite
  file on the desktop, ``IGNI_DATABASE_URL`` (PostgreSQL) in the cloud.
- :class:`igni_core.core.db.engine_registry.EngineRegistry` caches engines
  and sessionmakers per database; the process's one registry is
  ``igni_core.core.db.engine.engines``.
- :class:`igni_core.core.db.locator.StorageLocator` turns settings into
  :class:`igni_core.core.db.database.Database` handles, each with the
  :class:`igni_core.core.db.scope.StorageScope` (org, producer, project)
  every query filters by.
- :class:`igni_core.core.db.upsert.ConflictInsert` is ``INSERT … ON
  CONFLICT`` for both dialects.
- Submodule imports of model modules attach their tables to
  ``Base.metadata`` — alembic ``--autogenerate`` and the programmatic
  upgrade in :mod:`igni_core.core.db.migrations` rely on those imports
  happening before the metadata is read (``migrations/env.py``).
"""
