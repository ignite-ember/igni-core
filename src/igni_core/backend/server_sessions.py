"""Session management RPCs — list / switch / auto-name / search.

Owns :class:`SessionsController`, constructed with the underlying
:class:`Session` and a chat-history rebuilder callable. Every RPC
is a method on the class — the pre-refactor free-function shims
that took ``BackendServer`` as their first arg (Rule 6) have been
retired.

* :meth:`SessionsController.list_sessions` — enumerate all
  sessions for the project.
* :meth:`SessionsController.maybe_auto_name_session` — post-run
  auto-name pass, returning a typed :class:`AutoNameResult`.
* :meth:`SessionsController.switch_session` — flip the active
  session id + reload Agno's persisted history via
  :meth:`Session.rebind_identity`.
* :meth:`SessionsController.search_chat` — case-insensitive
  substring search across the persisted history.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel

from igni_core.backend.chat_history_searcher import ChatHistorySearcher
from igni_core.backend.schemas_history import ChatHistoryEntry, ChatSearchHit, GlobalChatSearchHit
from igni_core.backend.schemas_sessions import AutoNameResult
from igni_core.core.session.schemas import PersistResult
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.backend.schemas_history import ChatTurn
    from igni_core.core.session import Session

logger = logging.getLogger(__name__)


class SessionsController:
    """Session lifecycle RPCs for one :class:`Session`."""

    def __init__(
        self,
        session: Session,
        chat_history_provider: Callable[
            [str], Awaitable[list[ChatTurn] | list[ChatHistoryEntry] | list[dict]]
        ],
        waiting_sessions: Callable[[], set[str]] | None = None,
        session_dir_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self._session = session
        # session_id → the project folder it is bound to, so the sidebar
        # can group chats by folder. Global registry; a callable so this
        # class does not own the store. None → no folder is reported.
        self._session_dir_lookup = session_dir_lookup
        # :meth:`ChatHistoryRebuilder.rebuild` in production; injected
        # as a callable so this class does not own the rebuilder.
        # :meth:`search_chat` normalises whatever it returns to
        # :class:`ChatHistoryEntry`.
        self._chat_history_provider = chat_history_provider
        # The sessions that have an approval waiting on the person, as
        # a callable so this class does not own the journal. It is the
        # journal's answer rather than the running session's: a session
        # that is not loaded right now still waits on somebody, and a
        # person who just restarted the backend still has to be told.
        self._waiting_sessions = waiting_sessions

    async def list_sessions(self) -> msg.SessionListResult:
        """List all sessions for this project. No cap — the FE has
        virtualisation."""
        rows = await self._session.persistence.list_sessions(limit=None)
        waiting = self._paused_sessions()

        def folder(session_id: str) -> str:
            if self._session_dir_lookup is None:
                return ""
            return self._session_dir_lookup(session_id) or ""

        # The wire boundary is the one place a persistence row turns
        # into JSON — the typed row in, the wire entry out. ``project_dir``
        # rides along as an extra field (``SessionListEntry`` allows them)
        # so the sidebar can group chats by folder.
        return msg.SessionListResult(
            sessions=[
                msg.SessionListEntry(
                    **row.model_dump(),
                    has_paused_run=row.session_id in waiting,
                    project_dir=folder(row.session_id),
                )
                for row in rows
            ]
        )

    def _paused_sessions(self) -> set[str]:
        """The sessions with a run waiting for the person to answer.

        An empty set when nothing knows: a backend built without a
        journal (a test) says no session is waiting, rather than
        failing the whole list.
        """
        if self._waiting_sessions is None:
            return set()
        try:
            return self._waiting_sessions()
        except Exception as exc:
            logger.debug("session list: could not read the paused sessions: %s", exc)
            return set()

    async def maybe_auto_name_session(self, *, model_busy: bool = False) -> AutoNameResult:
        """Auto-generate a name for the current session if it has none.

        Returns a typed :class:`AutoNameResult` — the dispatcher
        reads ``.name`` when ``.ok`` is true and forwards the FE
        push, matching the previous ``str | None`` semantics
        byte-for-byte.

        Sanitisation (stripping model-emitted markdown wrappers
        like ``**Title**`` / ``# Title`` / ``"Title"``) lives inside
        :meth:`SessionPersistence.auto_name` — callers of this
        method never see the raw wrapped form. ``model_busy`` (a run
        is in progress) titles the session without asking the model.
        """
        persistence = self._session.persistence
        try:
            existing = await persistence.get_name()
            if existing:
                return AutoNameResult(ok=False, name=existing, reason="already_named")
            name = await persistence.auto_name(self._session.main_team.model, model_busy=model_busy)
        except Exception as exc:
            logger.debug("session auto-name failed: %s", exc)
            return AutoNameResult(ok=False, name="", reason="error")
        if not name:
            return AutoNameResult(ok=False, name="", reason="no_name_produced")
        return AutoNameResult(ok=True, name=name, reason="generated")

    async def title(self, name: str) -> PersistResult:
        """Name this session now — before its first run, too — so no auto-name replaces it."""
        result = await self._session.persistence.title(name)
        if result.ok:
            self._session.identity.mark_named()
        return result

    async def switch_session(self, session_id: str) -> msg.Info:
        """Switch to a different session.

        The four-attribute swap invariant (``session_id`` +
        ``session_named`` + ``main_team.session_id`` +
        ``persistence.session_id``) plus the ``aget_session``
        history reload live on :meth:`Session.rebind_identity` —
        this controller is a one-liner into that method.
        """
        await self._session.rebind_identity(session_id)
        name = await self._session.persistence.get_name()
        return msg.Info(text=f"Switched to session: {name or session_id}")

    async def search_chat(
        self, session_id: str, query: str, limit: int = 50
    ) -> list[ChatSearchHit]:
        """Case-insensitive substring search across the persisted
        history of ``session_id``.

        Emission order is defined by
        :meth:`ChatHistoryRebuilder.rebuild` — grep for that class
        when changing the parallel ``historyIndex -> itemIndex`` map
        on the FE side.
        """
        needle = (query or "").strip()
        if not needle:
            return []
        raw_history = await self._chat_history_provider(session_id)
        history = [self._as_entry(entry) for entry in raw_history]
        return ChatHistorySearcher(history).search(needle, limit=limit)

    async def search_all_chats(self, query: str, limit: int = 50) -> list[GlobalChatSearchHit]:
        """Case-insensitive substring search across every session in the
        project, newest session first, capped at ``limit`` hits in total.

        Each hit carries its ``session_id`` and ``session_name`` so the
        client can open the right conversation; the rest of the shape
        matches :meth:`search_chat`.
        """
        needle = (query or "").strip()
        if not needle:
            return []
        listing = await self._session.persistence.list_sessions(limit=None)
        rows = listing.value if listing.ok else []
        out: list[GlobalChatSearchHit] = []
        for row in rows:
            if len(out) >= limit:
                break
            try:
                raw_history = await self._chat_history_provider(row.session_id)
            except Exception as exc:
                logger.debug("search_all_chats: skipping %s (%s)", row.session_id, exc)
                continue
            history = [self._as_entry(entry) for entry in raw_history]
            hits = ChatHistorySearcher(history).search(needle, limit=limit - len(out))
            for hit in hits:
                out.append(
                    GlobalChatSearchHit(
                        session_id=row.session_id,
                        session_name=row.name or "",
                        **hit.model_dump(),
                    )
                )
                if len(out) >= limit:
                    break
        return out

    @staticmethod
    def _as_entry(entry: ChatTurn | ChatHistoryEntry | dict) -> ChatHistoryEntry:
        """One history turn as the searcher's :class:`ChatHistoryEntry`.

        A :class:`ChatTurn` goes through its JSON dump — the same shape
        the wire carries — because validating the model instance
        directly would collapse its per-role fields.
        """
        if isinstance(entry, ChatHistoryEntry):
            return entry
        if isinstance(entry, BaseModel):
            return ChatHistoryEntry.model_validate(entry.model_dump(mode="json"))
        return ChatHistoryEntry.model_validate(entry)
