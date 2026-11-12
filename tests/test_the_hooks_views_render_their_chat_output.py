"""``HooksReloadResult`` and ``HooksListView`` render the /hooks chat output."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_hooks import HooksListView, HooksReloadResult
from igni_core.core.hooks.schemas import HookDefinition
from igni_core.protocol.messages import CommandResultKind


def test_a_reload_says_how_many_hooks_loaded():
    result = HooksReloadResult(count=0).to_command_result()
    assert result.content == "Hooks reloaded. 0 hook(s) loaded."
    assert result.kind is CommandResultKind.INFO


def test_an_empty_map_is_an_info_result():
    result = HooksListView(hooks_map={}).to_command_result()
    assert (result.kind, result.content) == (CommandResultKind.INFO, "No hooks loaded.")


def test_each_hook_is_a_line_with_the_event_in_bold_and_the_command_in_code():
    view = HooksListView(
        hooks_map={
            "PreToolUse": [
                HookDefinition(type="command", command="a.sh", matcher="Bash"),
                HookDefinition(type="command", command="b.sh"),
            ],
            "Stop": [HookDefinition(type="command", command="c.sh", matcher="")],
        }
    )
    result = view.to_command_result()
    assert result.kind is CommandResultKind.MARKDOWN
    assert result.content == (
        "## Hooks\n"
        "- **PreToolUse**: `a.sh` (matcher: Bash)\n"
        "- **PreToolUse**: `b.sh`\n"
        "- **Stop**: `c.sh`\n"
    )


def test_a_hook_with_a_url_and_no_command_shows_the_url():
    hook = HookDefinition(type="http", url="https://h.example/x")
    content = HooksListView(hooks_map={"Stop": [hook]}).to_command_result().content
    assert "- **Stop**: `https://h.example/x`\n" in content


def test_the_command_wins_over_the_url():
    hook = HookDefinition(type="command", command="run.sh", url="https://h.example/x")
    content = HooksListView(hooks_map={"Stop": [hook]}).to_command_result().content
    assert "`run.sh`" in content and "h.example" not in content


def test_an_event_with_no_hooks_lists_only_the_header():
    result = HooksListView(hooks_map={"Stop": []}).to_command_result()
    assert result.content == "## Hooks\n"


def test_markdown_in_a_command_is_shown_as_it_is():
    hook = HookDefinition(type="command", command="echo `x` **y**")
    content = HooksListView(hooks_map={"Stop": [hook]}).to_command_result().content
    assert "`echo `x` **y**`" in content


def test_the_map_holds_hook_definitions_and_nothing_else():
    view = HooksListView(hooks_map={"Stop": [{"type": "command", "command": "x"}]})
    assert isinstance(view.hooks_map["Stop"][0], HookDefinition)
    with pytest.raises(ValidationError):
        HooksListView(hooks_map={"Stop": ["just a string"]})
