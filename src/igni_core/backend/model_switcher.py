"""Model switching for the window's model picker.

:class:`ModelSwitcher` owns the ``switch_model`` request: it swaps the
session's default model through :meth:`Session.set_default_model` and
persists the choice at both the user level and the session level,
telling the user when either save didn't land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.config.user_config_store import UserConfigStore
    from igni_core.core.session import Session
    from igni_core.core.session.session_preferences import SessionPreferencesStore


class ModelSwitcher:
    """Switch the active model + persist both user-level and
    per-session preferences."""

    def __init__(
        self,
        session: Session,
        session_prefs: SessionPreferencesStore,
        user_config_store: UserConfigStore,
    ) -> None:
        self._session = session
        self._session_prefs = session_prefs
        self._user_config_store = user_config_store

    def switch(self, model_name: str) -> msg.Info | msg.Error:
        """Switch the active model and persist the choice.

        Two layers of persistence so the choice survives both an
        app restart AND a session resume:

        * **User-level default** — ``~/.igni/config.yaml`` via
          :class:`UserConfigStore`, so the next launch opens on it.
        * **Per-session preference** — ``state.db``'s
          ``ember_session_preferences`` table, which ``--continue``
          consults on startup.

        A model the registry doesn't know is refused, as ``/model``
        refuses it. A failed save doesn't undo the switch (the
        in-memory state is already updated); the reply says which
        save didn't land.
        """
        models = self._session.settings.models
        old_vision = models.has_vision(models.default)

        result = self._session.set_default_model(model_name)
        if not result.ok:
            return msg.Error(text=result.failure_text)

        lines = [f"Switched to {model_name}"]
        if not self._user_config_store.set_default_model(model_name):
            lines.append("Note: couldn't save it as the default for new sessions.")
        if not self._session_prefs.set_model(self._session.session_id, model_name):
            lines.append("Note: couldn't save it for this session; --continue won't restore it.")
        # Warn if switching from vision to non-vision with media in
        # history.
        if old_vision and not models.has_vision(model_name):
            lines.append(
                "Note: previous messages may contain images/files. "
                "Use /clear to reset if you get errors."
            )
        return msg.Info(text="\n".join(lines))
