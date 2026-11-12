"""The panel RPCs' wire models: hook rows, the slash-command catalog, ephemeral-agent results.

A header's value and a URL's credentials never reach a hook row; that is pinned
in ``test_no_result_carries_a_secret.py``. This file pins the rest of what the
client reads from them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from igni_core.backend.schemas_panels import (
    BuiltinSlashCommand,
    DiscardEphemeralResult,
    ExtensionSlashCommand,
    HookEntryView,
    MarkdownSlashCommand,
    OutputStyleInfo,
    OutputStylesResult,
    PromoteEphemeralResult,
    SkillSlashCommand,
    SlashCommandEntry,
)
from igni_core.core.contributed_commands import ContributedCommand
from igni_core.core.hooks.schemas import HookDefinition

CATALOG = TypeAdapter(list[SlashCommandEntry])


def test_a_hook_row_carries_what_the_hook_runs_and_when() -> None:
    hook = HookDefinition(
        type="command", command="make lint", matcher="Bash", timeout=3000, background=True
    )

    assert HookEntryView.from_hook(hook, "PostToolUse").model_dump() == {
        "event": "PostToolUse",
        "type": "command",
        "command": "make lint",
        "url": "",
        "matcher": "Bash",
        "timeout_ms": 3000,
        "background": True,
        "header_names": [],
    }


def test_a_hook_without_a_timeout_reads_as_zero_milliseconds() -> None:
    hook = SimpleNamespace(type="command", command="x", timeout=None)

    assert HookEntryView.from_hook(hook, "Stop").timeout_ms == 0  # type: ignore[arg-type]


def test_a_hook_object_from_an_older_session_still_makes_a_row() -> None:
    old = SimpleNamespace(type="command", command="echo hi")

    view = HookEntryView.from_hook(old, "SessionStart")  # type: ignore[arg-type]

    assert view.model_dump() == {
        "event": "SessionStart",
        "type": "command",
        "command": "echo hi",
        "url": "",
        "matcher": "",
        "timeout_ms": 0,
        "background": False,
        "header_names": [],
    }


def test_a_hook_with_every_optional_field_none_makes_an_empty_row() -> None:
    hook = SimpleNamespace(
        type=None, command=None, url=None, matcher=None, timeout=None, background=None, headers=None
    )

    view = HookEntryView.from_hook(hook, "Stop")  # type: ignore[arg-type]

    assert view.model_dump() == {
        "event": "Stop",
        "type": "",
        "command": "",
        "url": "",
        "matcher": "",
        "timeout_ms": 0,
        "background": False,
        "header_names": [],
    }


def test_each_slash_command_source_builds_its_own_entry() -> None:
    md = SimpleNamespace(name="deploy", description="Ship it", argument_hint="<env>")
    skill = SimpleNamespace(name="review", description="Review code")
    skill_hinted = SimpleNamespace(name="plan", description="Plan", argument_hint="<goal>")
    contributed = ContributedCommand(
        name="compliance",
        description="Scan",
        owner="acme.scanner",
        invoke=lambda *_a, **_k: None,  # type: ignore[arg-type]
    )

    entries = [
        BuiltinSlashCommand.from_builtin("help", "Show help"),
        MarkdownSlashCommand.from_markdown(md),  # type: ignore[arg-type]
        SkillSlashCommand.from_skill(skill),  # type: ignore[arg-type]
        SkillSlashCommand.from_skill(skill_hinted),  # type: ignore[arg-type]
        ExtensionSlashCommand.from_contributed(contributed),
    ]

    assert [e.model_dump() for e in entries] == [
        {"source": "builtin", "name": "help", "description": "Show help", "argument_hint": ""},
        {
            "source": "markdown",
            "name": "deploy",
            "description": "Ship it",
            "argument_hint": "<env>",
        },
        {"source": "skill", "name": "review", "description": "Review code", "argument_hint": ""},
        {"source": "skill", "name": "plan", "description": "Plan", "argument_hint": "<goal>"},
        {
            "source": "extension",
            "name": "compliance",
            "description": "Scan",
            "argument_hint": "",
            "owner": "acme.scanner",
        },
    ]


def test_a_catalog_read_from_json_gets_each_entry_its_class_by_source() -> None:
    entries = CATALOG.validate_python(
        [
            {"source": "builtin", "name": "a"},
            {"source": "markdown", "name": "b"},
            {"source": "skill", "name": "c"},
            {"source": "extension", "name": "d", "owner": "o"},
        ]
    )

    assert [type(e) for e in entries] == [
        BuiltinSlashCommand,
        MarkdownSlashCommand,
        SkillSlashCommand,
        ExtensionSlashCommand,
    ]
    assert entries[3].owner == "o"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "raw",
    [
        {"source": "plugin", "name": "x"},
        {"name": "x"},
        {"source": "builtin"},
        {"source": "skill", "name": "x", "owner": "ignored-but-fine", "argument_hint": 3},
    ],
)
def test_a_catalog_entry_with_an_unknown_source_or_a_missing_name_is_refused(
    raw: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CATALOG.validate_python([raw])


def test_the_output_styles_result_names_the_active_style_and_each_choice() -> None:
    result = OutputStylesResult(
        active="terse",
        styles=[
            OutputStyleInfo(name="terse", description="Short answers"),
            OutputStyleInfo(name="plain", description=""),
        ],
    )

    assert result.model_dump() == {
        "active": "terse",
        "styles": [
            {"name": "terse", "description": "Short answers"},
            {"name": "plain", "description": ""},
        ],
    }


def test_a_promote_that_worked_names_the_file_and_one_that_failed_the_reason() -> None:
    assert PromoteEphemeralResult(ok=True, name="scout", dest="/p/scout.md").model_dump() == {
        "ok": True,
        "name": "scout",
        "dest": "/p/scout.md",
        "reason": "",
    }
    assert PromoteEphemeralResult(ok=False, name="ünï", reason="exists").model_dump() == {
        "ok": False,
        "name": "ünï",
        "dest": "",
        "reason": "exists",
    }


def test_a_discard_result_has_no_destination() -> None:
    assert DiscardEphemeralResult(ok=False, name="scout", reason="no such agent").model_dump() == {
        "ok": False,
        "name": "scout",
        "reason": "no such agent",
    }
    assert DiscardEphemeralResult(ok=True, name="scout").reason == ""


def test_every_field_of_the_panel_models_is_documented_for_the_sdks() -> None:
    for model in (
        OutputStyleInfo,
        OutputStylesResult,
        HookEntryView,
        BuiltinSlashCommand,
        MarkdownSlashCommand,
        SkillSlashCommand,
        ExtensionSlashCommand,
        PromoteEphemeralResult,
        DiscardEphemeralResult,
    ):
        schema = model.model_json_schema()
        assert schema["description"], model.__name__
        assert all(p.get("description") for p in schema["properties"].values()), model.__name__
