"""Resume-session lookup.

Owns the ``StorageManager.build_db → SessionPersistence → list_sessions`` triple
that used to sit inline in the CLI callback. Returns a typed
:class:`ResumeLookup` so the caller can distinguish "no previous
sessions found" from "DB unreachable" — the pre-refactor code
swallowed both into the same message, which hid real errors.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from igni_core.core.config.settings import Settings
from igni_core.core.memory.manager import StorageManager
from igni_core.core.session.persistence.listing import SessionListing


class ResumeLookup(BaseModel):
    """Outcome of :meth:`ResumeResolver.latest_id`.

    Exactly one of the following states holds:

    * ``session_id`` is set — a previous session was found.
    * ``session_id`` is ``None`` AND ``error`` is ``None`` — no
      previous sessions exist (fresh install / cleared DB).
    * ``error`` is set — DB lookup failed; the message is the
      exception's ``str(...)`` so the CLI can surface a hint.
    """

    session_id: str | None = None
    error: str | None = None


class ResumeResolver:
    """Resolve the most-recent session id for ``igni --continue``.

    Constructed with a :class:`Settings` snapshot so the DB path
    honours the same 5-tier merge the caller performed. Kept as a
    dedicated class (rather than a free function) because the
    "async lookup with typed result" contract has a real amount of
    behaviour behind it that shouldn't leak into the CLI callback.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def latest_id(self) -> ResumeLookup:
        """Return the most-recent session id, or a typed error.

        Runs the async ``list_sessions(limit=1)`` call synchronously
        via :func:`asyncio.run` — the CLI startup path has no event
        loop of its own, so a blocking wrapper is the right shape.
        The bare-Exception catch is intentional: any DB-layer
        failure (missing file, schema mismatch, permission error)
        must not abort session startup — the CLI reports it and
        continues without a resume id.
        """
        try:
            db = StorageManager.build_db(self._settings)
            listing = SessionListing(db).list_sessions(limit=1)
            try:
                listed = asyncio.run(listing)
            except BaseException:
                listing.close()  # not awaited when a loop is already running
                raise
        except Exception as exc:
            return ResumeLookup(error=str(exc))

        # The listing reports a read the database failed as ``ok=False`` rather
        # than raising; that is the error state, not "no previous sessions".
        if not listed.ok:
            return ResumeLookup(error=listed.error or "the session list could not be read")
        if not listed.value:
            return ResumeLookup()
        return ResumeLookup(session_id=listed.value[0].session_id)
