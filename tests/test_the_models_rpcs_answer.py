"""The model, permission and update RPCs answer from the real settings,
permission rules and update checker.

``switch_model`` answers the switcher's ``info``, ``toggle_verbose``
flips the routing display and says which way, ``get_display_config`` and
``get_model_registry`` read the session's settings, ``check_permission``
and ``save_permission_rule`` work on the project's real
``settings.local.json``, and ``check_for_update`` answers ``null`` when
update checks are switched off, without asking anyone.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from igni_core.backend.hitl_permissions import HitlPermissions
from igni_core.backend.rpc_handlers.models import ModelsRpcHandler
from igni_core.core.config.settings import Settings
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils import update_checker
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


def _handler(
    project: Path | None = None, settings: Settings | None = None, **backend: object
) -> ModelsRpcHandler:
    settings = settings or Settings()
    hitl = SimpleNamespace(
        permissions=HitlPermissions(
            SimpleNamespace(project_dir=project, permission_evaluator=None)  # type: ignore[arg-type]
        )
    )
    backend_ns = SimpleNamespace(
        settings=settings, controllers=SimpleNamespace(hitl=hitl), **backend
    )
    return ModelsRpcHandler(SimpleNamespace(backend=backend_ns))  # type: ignore[arg-type]


def _rules(project: Path) -> dict:
    path = project / CONFIG_DIR / "settings.local.json"
    return json.loads(path.read_text())["permissions"]


@pytest.fixture(autouse=True)
def _own_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The person's own settings must not decide a permission."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_a_model_switch_answers_the_info_the_switcher_gave() -> None:
    switched = MagicMock(return_value=msg.Info(text="Switched to fast"))
    handler = _handler(switch_model=switched)

    info = handler.methods()[RpcMethod.SWITCH_MODEL]({"model_name": "fast"})

    assert info.text == "Switched to fast"
    switched.assert_called_once_with("fast")


def test_a_refused_model_switch_fails_the_call_with_the_reason() -> None:
    refused = MagicMock(return_value=msg.Error(text="Model 'nope' is not in the registry"))
    handler = _handler(switch_model=refused)

    with pytest.raises(ValueError, match="'nope' is not in the registry"):
        handler.methods()[RpcMethod.SWITCH_MODEL]({"model_name": "nope"})

    refused.assert_called_once_with("nope")


def test_a_model_switch_without_a_name_is_refused() -> None:
    from igni_core.backend.rpc_binding import RpcArgsError

    switched = MagicMock()

    with pytest.raises(RpcArgsError, match="model_name"):
        _handler(switch_model=switched).methods()[RpcMethod.SWITCH_MODEL]({})

    switched.assert_not_called()


def test_toggling_verbose_says_the_new_setting_each_time() -> None:
    settings = Settings()
    before = settings.display.show_routing
    call = _handler(
        settings=settings, toggle_verbose=settings.display.toggle_show_routing
    ).methods()[RpcMethod.TOGGLE_VERBOSE]

    assert call({}) is (not before)
    assert settings.display.show_routing is (not before)
    assert call({}) is before


def test_the_display_config_is_the_sessions() -> None:
    settings = Settings()
    settings.display.tool_result_preview_lines = 7

    shown = _handler(settings=settings).methods()[RpcMethod.GET_DISPLAY_CONFIG]({})

    assert shown is settings.display
    assert shown.tool_result_preview_lines == 7


def test_the_model_registry_lists_the_configured_models() -> None:
    settings = Settings()

    registry = _handler(settings=settings).methods()[RpcMethod.GET_MODEL_REGISTRY]({})

    assert registry.default == settings.models.default
    assert registry.max_context_window == settings.models.max_context_window
    assert set(registry.registry) == set(settings.models.registry)


def test_a_saved_rule_decides_the_next_check(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    save = handler.methods()[RpcMethod.SAVE_PERMISSION_RULE]
    check = handler.methods()[RpcMethod.CHECK_PERMISSION]
    call = {
        "tool_name": "Bash",
        "func_name": "run_shell_command",
        "tool_args": {"command": "git status"},
    }

    assert save({"rule": "Bash(git status)", "level": "deny"}) is None

    assert _rules(tmp_path)["deny"] == ["Bash(git status)"]
    assert check(call) == "deny"
    assert check({**call, "tool_args": {"command": "git log"}}) != "deny"


def test_a_rule_saved_at_another_level_replaces_the_first(tmp_path: Path) -> None:
    save = _handler(tmp_path).methods()[RpcMethod.SAVE_PERMISSION_RULE]

    save({"rule": "Bash(make)", "level": "deny"})
    save({"rule": "Bash(make)", "level": "allow"})

    rules = _rules(tmp_path)
    assert rules.get("allow") == ["Bash(make)"]
    assert "Bash(make)" not in rules.get("deny", [])


def test_a_rule_that_could_not_be_written_fails_the_call(tmp_path: Path) -> None:
    (tmp_path / CONFIG_DIR).write_text("a file where the folder should be")
    save = _handler(tmp_path).methods()[RpcMethod.SAVE_PERMISSION_RULE]

    with pytest.raises(OSError):
        save({"rule": "Bash(make)", "level": "allow"})


def test_a_tool_nothing_speaks_of_is_asked_about(tmp_path: Path) -> None:
    level = _handler(tmp_path).methods()[RpcMethod.CHECK_PERMISSION](
        {"tool_name": "Frobnicate", "func_name": "frobnicate", "tool_args": {}}
    )

    assert level == "ask"


@pytest.mark.parametrize(("ttl", "air_gapped"), [(0, False), (86400, True)])
async def test_with_update_checks_switched_off_nothing_newer_is_reported(
    ttl: int, air_gapped: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        update_checker.SettingsLoader,
        "load",
        classmethod(lambda cls: Settings(update_check_ttl=ttl, air_gapped=air_gapped)),
    )
    monkeypatch.setattr(update_checker, "default_cache_path", lambda: tmp_path / ".update-check")
    handler = _handler()

    assert await handler.methods()[RpcMethod.CHECK_FOR_UPDATE]({}) is None
    assert not (tmp_path / ".update-check").exists()
