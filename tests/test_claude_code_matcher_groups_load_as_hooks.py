"""Claude Code's nested hook format loads, in settings and in a plugin.

Claude Code writes ``{"PostToolUse": [{"matcher": "*", "hooks": [{"type":
"command", "command": ...}]}]}``, and its plugins wrap that in
``{"hooks": {...}}``. igni parsed each group as one flat hook with an
empty command: it loaded with no warning and never ran (campaign R299),
though HOOKS.md and PLUGINS.md say Claude Code hooks work unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from igni_core.core.hooks.loader import HookLoader
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import MergeStrategy

_NESTED = {
    "PostToolUse": [
        {
            "matcher": "Bash|Write",
            "hooks": [
                {"type": "command", "command": "echo one"},
                {"type": "command", "command": "echo two", "matcher": "Edit"},
            ],
        }
    ],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo prompt"}]}],
}


def _plugin(tmp_path: Path, block: dict) -> Path:
    (tmp_path / "hooks").mkdir(parents=True)
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps(block))
    return tmp_path


def _commands(registry: HookRegistry) -> dict[str, list[tuple[str, str]]]:
    return {event: [(h.command, h.matcher) for h in hooks] for event, hooks in registry.raw.items()}


def test_a_matcher_group_becomes_one_hook_per_inner_entry():
    registry = HookRegistry()
    warnings = registry.merge_from_dict(
        _NESTED, source=Path("settings.json"), strategy=MergeStrategy.APPEND
    )

    assert warnings == []
    assert _commands(registry) == {
        "PostToolUse": [("echo one", "Bash|Write"), ("echo two", "Edit")],
        "UserPromptSubmit": [("echo prompt", "")],
    }


def test_a_plugin_file_nested_or_wrapped_loads_the_same(tmp_path):
    for i, block in enumerate([_NESTED, {"description": "x", "hooks": _NESTED}]):
        registry = HookRegistry()
        result = HookLoader().load_plugin_hooks(_plugin(tmp_path / str(i), block), registry)
        assert result.warnings == []
        assert registry.total_hooks == 3


def test_a_command_hook_with_no_command_is_a_warning_not_a_silent_hook():
    registry = HookRegistry()
    warnings = registry.merge_from_dict(
        {"Stop": [{"type": "command"}]}, source=Path("s.json"), strategy=MergeStrategy.APPEND
    )

    assert registry.total_hooks == 0
    assert warnings and "no command" in warnings[0].detail


def test_igni_flat_entries_are_unchanged():
    registry = HookRegistry()
    registry.merge_from_dict(
        {"PreToolUse": [{"type": "command", "command": "v.sh", "matcher": "save_file"}]},
        source=Path("s.json"),
        strategy=MergeStrategy.APPEND,
    )
    assert _commands(registry) == {"PreToolUse": [("v.sh", "save_file")]}
