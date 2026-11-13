"""What the hook schemas hold: a hook's block, its one-line description and digest,
the group mark, the safety result, the load warning and the wire payload.

These are read through the loader and the executor everywhere else; here the models
are asked directly, so a change to a default, a spelling or an invariant is seen at
its source.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.core.hooks.schemas import (
    HookDefinition,
    HookLoadWarning,
    HookPayload,
    HookResult,
    MergeStrategy,
    PermissionDecision,
    SafetyCheckResult,
)

# ── The permission vocabulary and the merge strategies ────────────────


@pytest.mark.parametrize(
    ("wire", "decision"),
    [
        ("allow", PermissionDecision.ALLOW),
        ("deny", PermissionDecision.DENY),
        ("ask", PermissionDecision.ASK),
        ("defer", PermissionDecision.DEFER),
        ("", PermissionDecision.NONE),
        ("ALLOW", PermissionDecision.NONE),
        ("maybe", PermissionDecision.NONE),
    ],
)
def test_a_wire_decision_is_read_and_an_unknown_one_is_no_opinion(wire, decision):
    assert PermissionDecision.from_wire(wire) is decision


def test_the_merge_strategies_are_append_and_prepend():
    assert {s.name: s.value for s in MergeStrategy} == {"APPEND": "append", "PREPEND": "prepend"}


# ── A hook's block ────────────────────────────────────────────────────


def test_a_hook_block_has_these_defaults():
    hook = HookDefinition(type="command")

    assert hook.command == "" and hook.url == "" and hook.text == ""
    assert hook.headers == {} and hook.mcp_args == {}
    assert hook.mcp_server == "" and hook.mcp_tool == "" and hook.matcher == ""
    assert hook.timeout == 10000
    assert hook.background is False and hook.async_rewake is False


def test_the_type_has_no_default_so_a_saved_file_always_carries_it():
    with pytest.raises(ValidationError):
        HookDefinition()  # type: ignore[call-arg]


def test_a_type_nobody_implements_still_validates():
    assert HookDefinition(type="agent").type == "agent"


def test_the_fields_are_typed():
    with pytest.raises(ValidationError):
        HookDefinition(type="http", headers={"a": 1})  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        HookDefinition(type="command", timeout="soon")  # type: ignore[arg-type]


def test_from_wire_defaults_the_type_to_command():
    assert HookDefinition.from_wire({"command": "ls"}).type == "command"


def test_from_wire_reads_the_camel_case_async_rewake():
    hook = HookDefinition.from_wire({"type": "command", "asyncRewake": True})

    assert hook.async_rewake is True


def test_from_wire_prefers_the_snake_case_key_when_both_are_given():
    hook = HookDefinition.from_wire({"async_rewake": False, "asyncRewake": True})

    assert hook.async_rewake is False


def test_from_wire_leaves_the_callers_block_as_it_was():
    raw = {"asyncRewake": True, "command": "ls"}

    HookDefinition.from_wire(raw)

    assert raw == {"asyncRewake": True, "command": "ls"}


def test_a_matcher_decides_which_target_a_hook_fires_for():
    assert HookDefinition(type="command").matches("anything")
    assert HookDefinition(type="command", matcher="read_file").matches("read_file")
    assert not HookDefinition(type="command", matcher="read_file").matches("grep")
    assert HookDefinition(type="command", matcher="read_file|grep").matches("grep")
    assert HookDefinition(type="command", matcher="^edit_.*").matches("edit_file")


# ── What it will do, in a line ────────────────────────────────────────


@pytest.mark.parametrize(
    ("hook", "line"),
    [
        (HookDefinition(type="command", command="./check.sh --fast"), "./check.sh --fast"),
        (
            HookDefinition(type="http", url="https://hooks.example/x"),
            "POST https://hooks.example/x",
        ),
        (
            HookDefinition(type="mcp_tool", mcp_server="lint", mcp_tool="run"),
            "MCP tool lint/run",
        ),
        (
            HookDefinition(type="prompt", text="run the linter"),
            "Add to the conversation: run the linter",
        ),
        (HookDefinition(type="agent"), "agent hook"),
    ],
)
def test_describe_says_what_each_kind_of_hook_does(hook, line):
    assert hook.describe() == line


# ── Its digest ────────────────────────────────────────────────────────


def test_the_digest_is_the_sha256_of_the_sorted_json_dump():
    hook = HookDefinition(type="command", command="ls", mcp_args={"b": 1, "a": {"y": 2, "x": 1}})

    expected = hashlib.sha256(
        json.dumps(hook.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()

    assert hook.digest() == expected


def test_equal_hooks_have_one_digest_whatever_the_key_order():
    one = HookDefinition(type="command", command="ls", mcp_args={"a": 1, "b": 2})
    other = HookDefinition(type="command", command="ls", mcp_args={"b": 2, "a": 1})

    assert one.digest() == other.digest()


@pytest.mark.parametrize(
    "change",
    [
        {"command": "ls -l"},
        {"url": "https://x"},
        {"headers": {"a": "b"}},
        {"text": "t"},
        {"mcp_server": "s"},
        {"mcp_tool": "t"},
        {"mcp_args": {"a": 1}},
        {"matcher": "grep"},
        {"timeout": 5},
        {"background": True},
        {"async_rewake": True},
        {"type": "http"},
    ],
)
def test_the_digest_changes_when_anything_the_hook_does_changes(change):
    base = HookDefinition(type="command", command="ls")

    assert base.model_copy(update=change).digest() != base.digest()


def test_the_digest_reads_non_ascii_text():
    hook = HookDefinition(type="prompt", text="vérifie le résultat — ✓")

    assert len(hook.digest()) == 64 and hook.digest() == hook.model_copy().digest()


# ── The group mark ────────────────────────────────────────────────────


def test_a_developers_hook_is_not_from_the_group():
    hook = HookDefinition(type="command")

    assert hook.from_group is False and hook.group_source == ""


def test_marking_a_hook_records_where_the_group_shipped_it_and_returns_it():
    hook = HookDefinition(type="command", command="ls")

    marked = hook.mark_from_group(Path("/etc/igni/hooks.json"))

    assert marked is hook
    assert hook.from_group is True and hook.group_source == "/etc/igni/hooks.json"


def test_a_group_hook_with_no_named_source_reads_as_group_policy():
    assert HookDefinition(type="command").mark_from_group("").group_source == "group policy"


def test_the_group_mark_is_not_part_of_the_hook_so_no_settings_file_can_set_it():
    hook = HookDefinition.from_wire({"command": "ls", "_group_source": "evil", "from_group": True})

    assert hook.from_group is False
    assert "_group_source" not in hook.model_dump()
    plain = HookDefinition(type="command", command="ls")
    assert (
        plain.mark_from_group("x").digest() == HookDefinition(type="command", command="ls").digest()
    )


# ── The safety result ─────────────────────────────────────────────────


def test_no_block_is_ok_and_not_blocked():
    result = SafetyCheckResult.no_block()

    assert result.ok is True and result.blocked is False and result.block_message == ""


def test_a_block_carries_its_message():
    result = SafetyCheckResult.block("protected path")

    assert result.ok is False and result.blocked is True
    assert result.block_message == "protected path"


def test_a_block_with_no_message_is_refused():
    with pytest.raises(ValueError, match="non-empty message"):
        SafetyCheckResult.block("")


# ── What a hook answers, and a load's warning ─────────────────────────


def test_a_hook_result_continues_with_no_opinion_by_default():
    result = HookResult()

    assert result.should_continue is True and result.message == ""
    assert result.permission_decision is PermissionDecision.NONE


def test_a_load_warning_from_a_path_keeps_it_as_text_and_round_trips():
    warning = HookLoadWarning.from_path(Path("/p/settings.json"), "invalid_json", "line 3")

    assert warning.source == "/p/settings.json"
    assert warning.kind == "invalid_json" and warning.detail == "line 3"
    assert HookLoadWarning.model_validate_json(warning.model_dump_json()) == warning


# ── The wire payload ──────────────────────────────────────────────────


class _Body(BaseModel):
    name: str
    note: str | None = None
    empty: str = ""
    nested: dict[str, int | None] = {}


def test_the_payload_puts_its_body_on_the_wire_without_unset_fields():
    payload = HookPayload(body=_Body(name="n", nested={"a": 1, "b": None}))

    assert payload.to_wire_dict() == {"name": "n", "empty": "", "nested": {"a": 1, "b": None}}


def test_the_payload_is_json_ready():
    class Body(BaseModel):
        where: Path

    wire = HookPayload(body=Body(where=Path("/a/b"))).to_wire_dict()

    assert wire == {"where": "/a/b"}
    json.dumps(wire)


def test_a_payload_cannot_be_changed_once_made():
    payload = HookPayload(body=_Body(name="n"))

    with pytest.raises(ValidationError):
        payload.body = _Body(name="other")  # type: ignore[misc]
