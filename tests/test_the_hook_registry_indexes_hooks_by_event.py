"""HookRegistry: the event index, its read and write surface, and the block parser."""

from __future__ import annotations

from pathlib import Path

from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import HookDefinition, MergeStrategy

SOURCE = Path("/somewhere/settings.json")


def _hook(command: str, **fields: object) -> HookDefinition:
    return HookDefinition(type="command", command=command, **fields)


def _wire(command: str, **fields: object) -> dict[str, object]:
    return {"type": "command", "command": command, **fields}


def _commands(hooks: list[HookDefinition]) -> list[str]:
    return [h.command for h in hooks]


# ── reading ────────────────────────────────────────────────────────


def test_a_fresh_registry_is_empty_and_for_event_of_an_unknown_event_is_empty():
    registry = HookRegistry.from_empty()

    assert registry.raw == {}
    assert registry.total_hooks == 0
    assert registry.for_event("Nope") == []
    assert registry.for_event_and_target("Nope", "Edit") == []


def test_raw_is_the_same_dict_every_time_and_the_one_injected():
    shared: dict[str, list[HookDefinition]] = {}
    registry = HookRegistry(shared)

    assert registry.raw is shared
    assert registry.raw is registry.raw

    registry.append("Stop", _hook("a"))

    assert _commands(shared["Stop"]) == ["a"]
    shared["Start"] = [_hook("b")]
    assert _commands(registry.for_event("Start")) == ["b"]


def test_an_injected_empty_dict_is_kept_not_replaced():
    shared: dict[str, list[HookDefinition]] = {}

    assert HookRegistry(shared).raw is shared


def test_total_hooks_sums_every_event():
    registry = HookRegistry.from_empty()
    registry.append("Stop", _hook("a"))
    registry.append("Stop", _hook("b"))
    registry.append("Start", _hook("c"))

    assert registry.total_hooks == 3


def test_for_event_and_target_returns_all_for_an_empty_target_and_filters_otherwise():
    registry = HookRegistry.from_empty()
    registry.append("PreToolUse", _hook("all"))
    registry.append("PreToolUse", _hook("edit", matcher="Edit"))
    registry.append("PreToolUse", _hook("write", matcher="Write|Read"))

    assert _commands(registry.for_event_and_target("PreToolUse")) == ["all", "edit", "write"]
    assert _commands(registry.for_event_and_target("PreToolUse", "")) == ["all", "edit", "write"]
    assert _commands(registry.for_event_and_target("PreToolUse", "Edit")) == ["all", "edit"]
    assert _commands(registry.for_event_and_target("PreToolUse", "Read")) == ["all", "write"]
    assert _commands(registry.for_event_and_target("PreToolUse", "Bash")) == ["all"]


def test_foreground_and_background_split_the_blocking_from_the_fire_and_forget():
    hooks = [_hook("a"), _hook("b", background=True), _hook("c"), _hook("d", background=True)]

    assert _commands(HookRegistry.foreground(hooks)) == ["a", "c"]
    assert _commands(HookRegistry.background(hooks)) == ["b", "d"]
    assert HookRegistry.foreground([]) == []
    assert HookRegistry.background([]) == []


# ── writing ────────────────────────────────────────────────────────


def test_append_adds_at_the_tail_and_prepend_at_the_head():
    registry = HookRegistry.from_empty()
    registry.append("Stop", _hook("one"))
    registry.append("Stop", _hook("two"))
    registry.prepend("Stop", _hook("zero"))
    registry.prepend("Other", _hook("only"))

    assert _commands(registry.for_event("Stop")) == ["zero", "one", "two"]
    assert _commands(registry.for_event("Other")) == ["only"]


# ── the block parser ───────────────────────────────────────────────


def test_merge_appends_a_block_in_declared_order_and_returns_no_warnings():
    registry = HookRegistry.from_empty()
    registry.append("Stop", _hook("existing"))

    warnings = registry.merge_from_dict(
        {"Stop": [_wire("a"), _wire("b")], "Start": [_wire("c")]},
        source=SOURCE,
        strategy=MergeStrategy.APPEND,
    )

    assert warnings == []
    assert _commands(registry.for_event("Stop")) == ["existing", "a", "b"]
    assert _commands(registry.for_event("Start")) == ["c"]


def test_merge_prepending_a_block_puts_each_hook_at_the_head_so_a_block_reverses():
    registry = HookRegistry.from_empty()
    registry.append("Stop", _hook("existing"))

    registry.merge_from_dict(
        {"Stop": [_wire("a"), _wire("b")]}, source=SOURCE, strategy=MergeStrategy.PREPEND
    )

    assert _commands(registry.for_event("Stop")) == ["b", "a", "existing"]


def test_merge_of_an_empty_block_or_an_empty_event_list_adds_nothing():
    registry = HookRegistry.from_empty()

    assert registry.merge_from_dict({}, source=SOURCE, strategy=MergeStrategy.APPEND) == []
    assert (
        registry.merge_from_dict({"Stop": []}, source=SOURCE, strategy=MergeStrategy.APPEND) == []
    )
    assert registry.total_hooks == 0


def test_merge_skips_an_event_that_is_not_a_list_and_warns_naming_it():
    registry = HookRegistry.from_empty()

    warnings = registry.merge_from_dict(
        {"Stop": {"type": "command"}, "Start": [_wire("kept")]},
        source=SOURCE,
        strategy=MergeStrategy.APPEND,
    )

    (warning,) = warnings
    assert (warning.kind, warning.source) == ("non_dict_block", str(SOURCE))
    assert "'Stop'" in warning.detail
    assert registry.for_event("Stop") == []
    assert _commands(registry.for_event("Start")) == ["kept"]


def test_merge_skips_a_hook_entry_that_is_not_an_object_and_keeps_its_siblings():
    registry = HookRegistry.from_empty()

    warnings = registry.merge_from_dict(
        {"Stop": [_wire("a"), "echo hi", None, ["x"], _wire("b")]},
        source=SOURCE,
        strategy=MergeStrategy.APPEND,
    )

    assert [w.kind for w in warnings] == ["non_dict_hook"] * 3
    assert all("'Stop'" in w.detail and w.source == str(SOURCE) for w in warnings)
    assert _commands(registry.for_event("Stop")) == ["a", "b"]


def test_merge_skips_an_invalid_hook_with_a_validation_warning_and_keeps_its_siblings():
    registry = HookRegistry.from_empty()

    warnings = registry.merge_from_dict(
        {"Stop": [_wire("a"), _wire("b", headers="nope"), _wire("c", timeout="soon"), _wire("d")]},
        source=SOURCE,
        strategy=MergeStrategy.APPEND,
    )

    assert [w.kind for w in warnings] == ["validation_error", "validation_error"]
    assert all("malformed hook in 'Stop'" in w.detail for w in warnings)
    assert _commands(registry.for_event("Stop")) == ["a", "d"]


def test_merge_marks_hooks_as_the_groups_only_when_asked():
    registry = HookRegistry.from_empty()

    registry.merge_from_dict(
        {"Stop": [_wire("mine")]}, source=SOURCE, strategy=MergeStrategy.APPEND
    )
    registry.merge_from_dict(
        {"Stop": [_wire("theirs")]},
        source=SOURCE,
        strategy=MergeStrategy.APPEND,
        from_group=True,
    )

    mine, theirs = registry.for_event("Stop")
    assert not mine.from_group
    assert theirs.from_group
    assert theirs.group_source == str(SOURCE)


def test_a_hook_entry_without_a_type_is_a_command_hook():
    registry = HookRegistry.from_empty()

    warnings = registry.merge_from_dict(
        {"Stop": [{"command": "echo hi"}]}, source=SOURCE, strategy=MergeStrategy.APPEND
    )

    (hook,) = registry.for_event("Stop")
    assert warnings == []
    assert (hook.type, hook.command) == ("command", "echo hi")
