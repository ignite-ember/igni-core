"""The model picker's switch: what it saves, what it says, what it refuses.

:class:`ModelSwitcher` swaps the session's default model and saves the
choice twice, in the user's config (the next launch) and in the session's
preferences (``--continue``). It is tested here with the real stores on a
temporary directory, so what is pinned is what was written, not that a
call was made. The session is a stand-in with the registry's own rule for
``set_default_model`` (the real one rebuilds a team).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from igni_core.backend.model_switcher import ModelSwitcher
from igni_core.backend.schemas_model import ModelSwitchResult
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.core.config.user_config_store import UserConfigStore
from igni_core.core.session.session_preferences import SessionPreferencesStore
from igni_core.protocol import messages as msg


class _Settings:
    def __init__(self) -> None:
        self.models = ModelsConfig(
            default="seer",
            registry={
                "seer": ModelRegistryEntry(model_id="seer", vision=True),
                "text": ModelRegistryEntry(model_id="text"),
                "other-seer": ModelRegistryEntry(model_id="other-seer", vision=True),
            },
        )


class _Session:
    session_id = "s-1"

    def __init__(self) -> None:
        self.settings = _Settings()

    def set_default_model(self, model_name: str) -> ModelSwitchResult:
        registry = self.settings.models.registry
        if model_name not in registry:
            return ModelSwitchResult(ok=False, model_name=model_name, available=list(registry))
        self.settings.models.default = model_name
        return ModelSwitchResult(ok=True, model_name=model_name)


class _Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.session = _Session()
        self.config_path = tmp_path / "home" / "config.yaml"
        self.user = UserConfigStore(self.config_path)
        self.prefs = SessionPreferencesStore(tmp_path / "state.db")
        self.switcher = ModelSwitcher(self.session, self.prefs, self.user)

    def saved_default(self) -> Any:
        if not self.config_path.is_file():
            return None
        return yaml.safe_load(self.config_path.read_text())["models"]["default"]


def _text(reply: msg.Info | msg.Error) -> str:
    return reply.text


def test_a_switch_is_saved_for_new_sessions_and_for_this_one(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    reply = rig.switcher.switch("text")

    assert isinstance(reply, msg.Info)
    assert reply.text.startswith("Switched to text")
    assert rig.session.settings.models.default == "text"
    assert rig.saved_default() == "text"
    assert rig.prefs.get_model("s-1") == "text"


def test_a_model_the_registry_does_not_know_is_refused_and_nothing_is_saved(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)

    reply = rig.switcher.switch("nonesuch")

    assert isinstance(reply, msg.Error)
    assert "Unknown model: 'nonesuch'." in reply.text
    assert "Available:" in reply.text and "seer" in reply.text
    assert rig.session.settings.models.default == "seer"
    assert rig.saved_default() is None
    assert rig.prefs.get_model("s-1") is None


def test_an_empty_name_is_refused_like_any_unknown_one(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    reply = rig.switcher.switch("")

    assert isinstance(reply, msg.Error)
    assert rig.saved_default() is None
    assert rig.prefs.get_model("s-1") is None


def test_only_the_user_level_save_failing_is_said_and_the_session_save_stands(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.config_path.mkdir(parents=True)  # a directory where the file should go

    reply = rig.switcher.switch("text")

    assert "couldn't save it as the default for new sessions" in _text(reply)
    assert "--continue" not in _text(reply)
    assert rig.session.settings.models.default == "text", "the switch itself stands"
    assert rig.prefs.get_model("s-1") == "text"


def test_only_the_session_save_failing_is_said_and_the_user_level_save_stands(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.prefs.set_model = lambda session_id, model_name: False  # type: ignore[method-assign]

    reply = rig.switcher.switch("text")

    assert "--continue won't restore it" in _text(reply)
    assert "default for new sessions" not in _text(reply)
    assert rig.saved_default() == "text"
    assert rig.session.settings.models.default == "text"


def test_both_saves_failing_says_both(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.config_path.mkdir(parents=True)
    rig.prefs.set_model = lambda session_id, model_name: False  # type: ignore[method-assign]

    rig.session.settings.models.default = "text"  # no vision note in the way
    lines = _text(rig.switcher.switch("other-seer")).splitlines()

    assert lines[0] == "Switched to other-seer"
    assert len(lines) == 3
    assert "default for new sessions" in lines[1]
    assert "--continue" in lines[2]


def test_leaving_a_vision_model_for_a_text_one_warns_about_images_in_history(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)  # default "seer" has vision

    reply = rig.switcher.switch("text")

    assert "previous messages may contain images/files" in reply.text
    assert "/clear" in reply.text


def test_the_other_vision_transitions_do_not_warn(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)

    assert _text(rig.switcher.switch("other-seer")) == "Switched to other-seer"  # vision -> vision
    rig.switcher.switch("text")  # leaves vision; warns, not asserted here
    assert _text(rig.switcher.switch("text")) == "Switched to text"  # text -> text
    assert _text(rig.switcher.switch("seer")) == "Switched to seer"  # text -> vision
