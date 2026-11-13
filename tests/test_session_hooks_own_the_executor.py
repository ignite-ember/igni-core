"""``SessionHooks`` is the one owner of a session's hooks.

Two behaviours changed when it took over from ``Session``:

* ``/hooks reload`` used to rebuild from the settings files alone and drop
  every hook a plugin contributed until the next plugin reload. Load and
  reload now share one build path, so plugin hooks survive a reload.
* The headless message handler held the executor by value; it now reads
  ``SessionHooks.executor``, which every rebuild replaces (see
  ``test_the_message_handler_reads_live_state.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from igni_core.core.config.settings import Settings
from igni_core.core.hooks.load_result import HookLoadResult
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import HookDefinition, HookLoadWarning
from igni_core.core.session.hooks import SessionHooks
from igni_core.core.session.mcp_resolver import MCPToolResolver
from igni_core.core.session.reminders import PendingReminderQueue


class _Loader:
    """Settings files holding one project hook."""

    def load(self) -> HookLoadResult:
        project = HookDefinition(type="command", command="echo project")
        return HookLoadResult(registry=HookRegistry({"Stop": [project]}), warnings=[])


class _Plugins:
    """One enabled plugin contributing one hook, prepended like the real loader."""

    def apply_to_hooks(self, hook_loader: Any, registry: HookRegistry, *, disabled: set[str]):
        registry.prepend("Stop", HookDefinition(type="command", command="echo plugin"))
        return HookLoadResult(registry=registry, warnings=[])


async def _nobody(*_: Any, **__: Any) -> bool | None:
    return None


def _hooks(tmp_path: Path, reminders: PendingReminderQueue | None = None) -> SessionHooks:
    hooks = SessionHooks(
        project_dir=tmp_path,
        settings=Settings(),
        group_hooks_dir=tmp_path / "group-hooks",
        reminders=reminders or PendingReminderQueue(),
        resolver=MCPToolResolver(lambda: None),
        ask=_nobody,
    )
    hooks.load(Settings(), _Plugins(), set())  # type: ignore[arg-type]
    hooks._loader = _Loader()  # type: ignore[assignment]
    hooks.reload()
    return hooks


def _commands(hooks: SessionHooks) -> list[str | None]:
    return [h.command for h in hooks.hooks_map.get("Stop", [])]


def test_plugin_hooks_survive_a_reload(tmp_path: Path):
    hooks = _hooks(tmp_path)

    assert hooks.reload() == 2
    assert _commands(hooks) == ["echo plugin", "echo project"]


def test_a_reload_builds_a_new_executor_on_the_new_map(tmp_path: Path):
    hooks = _hooks(tmp_path)
    before = hooks.executor

    hooks.reload()

    assert hooks.executor is not before
    assert hooks.executor.hooks is hooks.hooks_map


def test_a_reload_drops_reminders_the_old_executor_queued(tmp_path: Path):
    reminders = PendingReminderQueue()
    hooks = _hooks(tmp_path, reminders)
    reminders.queue("from before")

    hooks.reload()

    assert reminders.drain() == []


def _bare(tmp_path: Path, reminders: PendingReminderQueue | None = None) -> SessionHooks:
    return SessionHooks(
        project_dir=tmp_path,
        settings=Settings(),
        group_hooks_dir=tmp_path / "group-hooks",
        reminders=reminders or PendingReminderQueue(),
        resolver=MCPToolResolver(lambda: None),
        ask=_nobody,
    )


def test_a_reload_before_any_load_changes_nothing(tmp_path: Path):
    reminders = PendingReminderQueue()
    hooks = _bare(tmp_path, reminders)
    executor = hooks.executor
    reminders.queue("kept")

    assert hooks.reload() == 0
    assert hooks.executor is executor
    assert reminders.drain() == ["kept"]


def test_a_load_without_plugins_is_the_settings_files_alone(tmp_path: Path):
    hooks = _bare(tmp_path)
    hooks.load(Settings(), None, set())  # type: ignore[arg-type]
    hooks._loader = _Loader()  # type: ignore[assignment]

    assert hooks.reload() == 1
    assert _commands(hooks) == ["echo project"]


def test_the_disabled_plugins_are_handed_to_the_plugin_loader(tmp_path: Path):
    seen: list[set[str]] = []

    class _Spy(_Plugins):
        def apply_to_hooks(self, hook_loader: Any, registry: HookRegistry, *, disabled: set[str]):
            seen.append(disabled)
            return super().apply_to_hooks(hook_loader, registry, disabled=disabled)

    hooks = _bare(tmp_path)
    hooks.load(Settings(), _Spy(), {"off"})  # type: ignore[arg-type]
    hooks.reload()

    assert seen == [{"off"}, {"off"}]


def test_every_load_warning_is_logged_with_its_source(tmp_path: Path, caplog):
    class _Warn(_Loader):
        def load(self) -> HookLoadResult:
            result = super().load()
            result.warnings.append(
                HookLoadWarning(kind="invalid_json", source="settings.json", detail="no good")
            )
            return result

    class _PluginWarn(_Plugins):
        def apply_to_hooks(self, hook_loader, registry, *, disabled):
            return HookLoadResult(
                registry=registry,
                warnings=[HookLoadWarning(kind="os_error", source="p.json", detail="worse")],
            )

    hooks = _bare(tmp_path)
    hooks.load(Settings(), _PluginWarn(), set())  # type: ignore[arg-type]
    hooks._loader = _Warn()  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING):
        hooks.reload()

    assert "hook reload warning [invalid_json] from settings.json: no good" in caplog.text
    assert "hook reload warning [os_error] from p.json: worse" in caplog.text


def test_the_group_hook_mode_follows_the_settings_of_the_latest_load(tmp_path: Path):
    hooks = _bare(tmp_path)
    first = Settings()
    second = Settings()
    first.hooks.group = "run"
    second.hooks.group = "refuse"

    hooks.load(first, None, set())  # type: ignore[arg-type]
    before = hooks.gate._mode()
    hooks.load(second, None, set())  # type: ignore[arg-type]

    assert before == first.hooks.group
    assert hooks.gate._mode() == second.hooks.group
