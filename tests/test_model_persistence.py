"""Tests for model-selection persistence across app restarts.

Two layers cooperate:

* :class:`UserConfigStore` — writes to ``~/.igni/config.yaml`` so a
  fresh session opened after restart loads the chosen model.
* :class:`SessionPreferencesStore` — writes to ``state.db`` keyed by
  ``session_id`` so ``--continue`` restores the model the user was
  using in that specific session, even if the user has since picked
  a different default for new sessions.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sqlalchemy.exc import OperationalError

from igni_core.backend.server import BackendServer
from igni_core.core.config.settings import Settings
from igni_core.core.config.user_config_store import UserConfigStore
from igni_core.core.data_paths import state_db_path
from igni_core.core.db.database import Database
from igni_core.core.db.scope import StorageScope, Tenant
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.session_preferences import SessionPreferencesStore


def _save_default_model(name: str) -> None:
    """Thin adapter that mirrors the deleted ``save_default_model``
    shim so the existing test bodies read naturally after the OOP-
    refactor promoted the persistence logic onto
    :class:`UserConfigStore`."""
    UserConfigStore().set_default_model(name)


class TestSaveDefaultModel:
    """``UserConfigStore.set_default_model`` round-trips through
    ``~/.igni/config.yaml``."""

    def test_writes_default_to_user_config(self, tmp_path, monkeypatch):
        """Setting a default writes a minimal ``models.default`` key
        and nothing else — no synthetic registry, no clobber of
        unrelated keys."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        _save_default_model("MiniMax-M2.7")

        cfg_path = tmp_path / CONFIG_DIR / "config.yaml"
        assert cfg_path.exists()
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg == {"models": {"default": "MiniMax-M2.7"}}

    def test_preserves_unrelated_keys(self, tmp_path, monkeypatch):
        """Writing the default must not clobber other top-level keys
        the user has set (e.g. ``permissions``, ``display``)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg_dir = tmp_path / CONFIG_DIR
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "display": {"color_theme": "dark"},
                    "models": {"max_context_window": 200_000},
                }
            )
        )

        _save_default_model("gpt-7")

        cfg = yaml.safe_load((cfg_dir / "config.yaml").read_text())
        assert cfg["display"] == {"color_theme": "dark"}
        # Original key in the models block survives — only ``default``
        # gets added.
        assert cfg["models"]["max_context_window"] == 200_000
        assert cfg["models"]["default"] == "gpt-7"

    def test_overwrites_existing_default(self, tmp_path, monkeypatch):
        """A second call replaces the previous default — no append,
        no list."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        _save_default_model("first")
        _save_default_model("second")

        cfg = yaml.safe_load((tmp_path / CONFIG_DIR / "config.yaml").read_text())
        assert cfg["models"]["default"] == "second"

    def test_recovers_from_corrupt_models_block(self, tmp_path, monkeypatch):
        """If ``models`` exists but isn't a dict (e.g. someone wrote
        a list by hand), the helper must overwrite it rather than
        crash. The previous bad value is gone, which is acceptable —
        the alternative is the next launch failing to load config."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg_dir = tmp_path / CONFIG_DIR
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.yaml").write_text(yaml.safe_dump({"models": ["bogus", "list"]}))

        _save_default_model("MiniMax-M2.7")

        cfg = yaml.safe_load((cfg_dir / "config.yaml").read_text())
        assert cfg["models"] == {"default": "MiniMax-M2.7"}

    def test_reports_a_save_that_did_not_land(self, tmp_path):
        """A config path that can't be written comes back ``False``
        instead of raising, so the switch can tell the user."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")

        assert UserConfigStore(blocker / "config.yaml").set_default_model("m") is False

    def test_reports_a_save_that_landed(self, tmp_path):
        assert UserConfigStore(tmp_path / "config.yaml").set_default_model("m") is True


class TestSessionPreferencesStore:
    """Per-session model preference round-trips through SQLite."""

    def test_set_then_get_returns_model(self, tmp_path):
        """Basic upsert + read."""
        store = SessionPreferencesStore(tmp_path / "state.db")
        assert store.set_model("sess-a", "MiniMax-M2.7") is True
        assert store.get_model("sess-a") == "MiniMax-M2.7"

    def test_get_unknown_session_returns_none(self, tmp_path):
        """No row → ``None``, not an error."""
        store = SessionPreferencesStore(tmp_path / "state.db")
        assert store.get_model("never-set") is None

    def test_set_is_per_session(self, tmp_path):
        """Two sessions don't share preferences."""
        store = SessionPreferencesStore(tmp_path / "state.db")
        store.set_model("sess-a", "model-a")
        store.set_model("sess-b", "model-b")
        assert store.get_model("sess-a") == "model-a"
        assert store.get_model("sess-b") == "model-b"

    def test_set_upserts(self, tmp_path):
        """Repeat ``set_model`` overwrites — no duplicate rows, no
        history."""
        store = SessionPreferencesStore(tmp_path / "state.db")
        store.set_model("sess", "old")
        store.set_model("sess", "new")
        assert store.get_model("sess") == "new"

    def test_survives_reopen(self, tmp_path):
        """Closing and reopening the store yields the same value —
        i.e. the data is genuinely on disk, not just in memory."""
        db_path = tmp_path / "state.db"
        SessionPreferencesStore(db_path).set_model("sess", "MiniMax-M2.7")
        # Fresh instance — new connection, no shared cache.
        assert SessionPreferencesStore(db_path).get_model("sess") == "MiniMax-M2.7"

    @pytest.mark.asyncio
    async def test_async_wrappers(self, tmp_path):
        store = SessionPreferencesStore(tmp_path / "state.db")
        await store.aset_model("sess", "MiniMax-M2.7")
        assert await store.aget_model("sess") == "MiniMax-M2.7"

    @staticmethod
    def _locked(store: SessionPreferencesStore) -> None:
        def refuse():
            raise OperationalError("select 1", {}, RuntimeError("database is locked"))

        store._db.sync_session = refuse  # type: ignore[method-assign]

    def test_a_save_that_cannot_land_answers_false_and_keeps_the_old_choice(self, tmp_path):
        """The in-memory switch has happened; the store tells the caller the
        choice will not survive a resume instead of raising."""
        db = tmp_path / "state.db"
        SessionPreferencesStore(db).set_model("sess", "old-model")
        store = SessionPreferencesStore(db)
        self._locked(store)

        assert store.set_model("sess", "new-model") is False
        assert SessionPreferencesStore(db).get_model("sess") == "old-model"

    def test_a_read_that_cannot_land_answers_none(self, tmp_path):
        db = tmp_path / "state.db"
        SessionPreferencesStore(db).set_model("sess", "m")
        store = SessionPreferencesStore(db)
        self._locked(store)

        assert store.get_model("sess") is None

    @pytest.mark.asyncio
    async def test_the_async_wrappers_report_the_same_failures(self, tmp_path):
        store = SessionPreferencesStore(tmp_path / "state.db")
        self._locked(store)

        assert await store.aset_model("sess", "m") is False
        assert await store.aget_model("sess") is None

    def test_projects_of_one_producer_keep_their_own_choice(self, tmp_path):
        scope = StorageScope.for_tenant(Tenant(org="acme", producer="person:ada"))
        db = Database(tmp_path / "shared.db", scope=scope)
        alpha = SessionPreferencesStore(db.in_scope(scope.model_copy(update={"project": "alpha"})))
        beta = SessionPreferencesStore(db.in_scope(scope.model_copy(update={"project": "beta"})))

        alpha.set_model("sess", "model-a")

        assert alpha.get_model("sess") == "model-a"
        assert beta.get_model("sess") is None


class TestBackendServerResumeOverride:
    """On ``--continue``, ``BackendServer.__init__`` must consult the
    per-session store and override the default model BEFORE
    ``Session.__init__`` builds the main team.

    The override belongs to the resumed session alone: the settings
    the caller handed over are the ones the session pool copies for
    every other session, so a resumed session's model must not become
    their default."""

    @staticmethod
    def _make_settings(tmp_path, registry):
        settings = Settings()
        settings.storage.data_dir = str(tmp_path / "ember")
        settings.models.default = "user-level-default"
        settings.models.registry = registry
        return settings

    @staticmethod
    def _construct_server(settings, project_dir, captured, **kwargs):
        """Run ``BackendServer.__init__`` with Session mocked.

        Records the value of ``settings.models.default`` of the
        settings the Session was *given* — that's the only thing
        these tests care about. The returned mock has a real
        ``project_dir`` so the downstream PendingMessageStore call
        succeeds.
        """

        def fake_session(*args, **kw):
            captured["default"] = args[0].models.default
            m = MagicMock()
            m.project_dir = project_dir
            return m

        with patch("igni_core.backend.backend_bootstrap.Session", side_effect=fake_session):
            BackendServer(settings, project_dir=project_dir, **kwargs)

    def test_resume_overrides_default_with_persisted_choice(self, tmp_path):
        """The model the user was on last time wins over the
        user-level default."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = state_db_path(project_dir, data_dir=str(tmp_path / "ember"))
        SessionPreferencesStore(db_path).set_model("resumed-sess", "per-session-model")

        settings = self._make_settings(
            tmp_path,
            {
                "user-level-default": {"url": "x", "model_id": "x"},
                "per-session-model": {"url": "y", "model_id": "y"},
            },
        )

        captured: dict = {}
        self._construct_server(
            settings,
            project_dir,
            captured,
            resume_session_id="resumed-sess",
        )
        assert captured["default"] == "per-session-model"

    def test_the_callers_settings_are_not_mutated(self, tmp_path):
        """The caller's settings are the pool's, and the pool copies
        them for every session it resumes. A resumed session's model
        must not become the default those copies start on."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = state_db_path(project_dir, data_dir=str(tmp_path / "ember"))
        SessionPreferencesStore(db_path).set_model("resumed-sess", "per-session-model")

        settings = self._make_settings(
            tmp_path,
            {
                "user-level-default": {"url": "x", "model_id": "x"},
                "per-session-model": {"url": "y", "model_id": "y"},
            },
        )

        captured: dict = {}
        self._construct_server(
            settings,
            project_dir,
            captured,
            resume_session_id="resumed-sess",
        )
        assert settings.models.default == "user-level-default", (
            "the caller's settings were changed in place"
        )

    def test_resume_falls_back_when_persisted_model_missing_from_registry(self, tmp_path):
        """Stale row pointing at a model no longer in the registry
        must NOT override — the user would get a broken team."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = state_db_path(project_dir, data_dir=str(tmp_path / "ember"))
        SessionPreferencesStore(db_path).set_model("resumed-sess", "deleted-model")

        settings = self._make_settings(
            tmp_path,
            {"user-level-default": {"url": "x", "model_id": "x"}},
        )

        captured: dict = {}
        self._construct_server(
            settings,
            project_dir,
            captured,
            resume_session_id="resumed-sess",
        )
        assert captured["default"] == "user-level-default"

    def test_fresh_session_does_not_consult_store(self, tmp_path):
        """No ``resume_session_id`` → no per-session lookup. The
        user-level default stays in place even if some other
        session in the same store has a different model."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = state_db_path(project_dir, data_dir=str(tmp_path / "ember"))
        SessionPreferencesStore(db_path).set_model("some-old-sess", "leftover-model")

        settings = self._make_settings(
            tmp_path,
            {
                "user-level-default": {"url": "x", "model_id": "x"},
                "leftover-model": {"url": "y", "model_id": "y"},
            },
        )

        captured: dict = {}
        self._construct_server(settings, project_dir, captured)
        assert captured["default"] == "user-level-default"
