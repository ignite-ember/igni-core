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

from pathlib import Path
from typing import Any

from igni_core.core.config.settings import Settings
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import HookDefinition, HookLoadResult
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
