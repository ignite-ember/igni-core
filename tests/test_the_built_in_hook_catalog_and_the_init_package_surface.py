"""What ``BUILT_IN_HOOKS`` ships, and what ``igni_core.core.init`` exports."""

from __future__ import annotations

import igni_core.core.init as init
from igni_core.core.init import BUILT_IN_HOOKS
from igni_core.core.paths import CONFIG_DIR


def _by_file() -> dict:
    return {spec.filename: spec for spec in BUILT_IN_HOOKS}


def test_exactly_two_hooks_ship_in_this_order():
    assert [s.filename for s in BUILT_IN_HOOKS] == ["pre-pr-review.sh", "post-commit-todo.sh"]


def test_the_pre_push_review_runs_before_a_bash_call_and_blocks_for_it():
    spec = _by_file()["pre-pr-review.sh"]

    assert spec.event == "PreToolUse"
    d = spec.definition
    assert (d.type, d.command, d.matcher, d.timeout) == (
        "command",
        f"{CONFIG_DIR}/hooks/pre-pr-review.sh",
        "Bash",
        15000,
    )
    assert d.background is False


def test_the_post_commit_todo_runs_after_a_bash_call_in_the_background():
    spec = _by_file()["post-commit-todo.sh"]

    assert spec.event == "PostToolUse"
    d = spec.definition
    assert (d.type, d.command, d.matcher, d.timeout) == (
        "command",
        f"{CONFIG_DIR}/hooks/post-commit-todo.sh",
        "Bash",
        15000,
    )
    assert d.background is True


def test_every_command_points_at_the_script_the_spec_writes():
    for spec in BUILT_IN_HOOKS:
        assert spec.definition.command == f"{CONFIG_DIR}/hooks/{spec.filename}"
        assert spec.content.startswith("#!")


def test_the_package_exports_exactly_its_public_surface_and_each_name_resolves():
    assert sorted(init.__all__) == [
        "BUILT_IN_HOOKS",
        "BuiltInHookSpec",
        "HomeConfigMigrator",
        "HookDefinition",
        "HookProvisioner",
        "InitConfig",
        "InitResult",
        "ProjectInitializer",
        "SettingsFile",
    ]
    for name in init.__all__:
        assert getattr(init, name) is not None


def test_the_removed_shim_is_not_exported():
    assert not hasattr(init, "initialize_project")
    assert not hasattr(init.ProjectInitializer, "initialize")
