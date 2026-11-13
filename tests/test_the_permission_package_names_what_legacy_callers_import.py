"""The permission package's public surface and its explain_deny shim.

Callers import the permission types, the catalog sets and the mode
strategies from the package itself; the names must stay there, and the
free ``explain_deny`` must give exactly what the evaluator's own does.
"""

from __future__ import annotations

import igni_core.core.config.permission_eval as package
from igni_core.core.config.permission_eval import (
    BashCommand,
    PermissionDecision,
    PermissionEvaluator,
    PermissionMode,
    ToolCategoryCatalog,
    explain_deny,
)
from igni_core.core.config.permission_eval import catalog as catalog_module
from igni_core.core.config.permission_eval import strategies as strategies_module

EXPECTED = {
    "PermissionMode",
    "PermissionDecision",
    "PermissionOutcome",
    "PermissionOutcomeSource",
    "PermissionRule",
    "PermissionEvaluator",
    "explain_deny",
    "FILE_EDIT_TOOLS",
    "FILE_READ_TOOLS",
    "SHELL_TOOLS",
    "BashCommand",
    "ToolCategoryCatalog",
    "FriendlyToolNameResolver",
    "PermissionModeStrategy",
    "DefaultMode",
    "DontAskMode",
    "AcceptEditsMode",
    "BypassPermissionsMode",
    "PlanMode",
}


def test_all_names_exactly_what_legacy_callers_import():
    assert set(package.__all__) == EXPECTED
    assert len(package.__all__) == len(EXPECTED)


def test_every_listed_name_resolves_on_the_package():
    assert all(hasattr(package, name) for name in package.__all__)


def test_the_catalog_names_are_the_catalog_module_s_own_objects():
    assert package.BashCommand is catalog_module.BashCommand
    assert package.FILE_EDIT_TOOLS is catalog_module.FILE_EDIT_TOOLS
    assert package.FILE_READ_TOOLS is catalog_module.FILE_READ_TOOLS
    assert package.SHELL_TOOLS is catalog_module.SHELL_TOOLS
    assert package.ToolCategoryCatalog is catalog_module.ToolCategoryCatalog


def test_the_strategies_are_the_strategy_module_s_own_classes():
    for name in (
        "DefaultMode",
        "DontAskMode",
        "AcceptEditsMode",
        "BypassPermissionsMode",
        "PlanMode",
    ):
        assert getattr(package, name) is getattr(strategies_module, name)


def test_a_mode_picks_the_strategy_of_that_name():
    picks = {
        PermissionMode.DEFAULT: package.DefaultMode,
        PermissionMode.DONT_ASK: package.DontAskMode,
        PermissionMode.ACCEPT_EDITS: package.AcceptEditsMode,
        PermissionMode.BYPASS_PERMISSIONS: package.BypassPermissionsMode,
        PermissionMode.PLAN: package.PlanMode,
    }
    for mode, strategy in picks.items():
        assert isinstance(PermissionEvaluator.from_strings(mode=mode)._strategy(), strategy)
        assert issubclass(strategy, package.PermissionModeStrategy)


def test_the_shim_gives_what_the_evaluator_gives_for_a_refused_edit():
    evaluator = PermissionEvaluator.from_strings(mode="plan")
    args = {"file_path": "a.py"}

    assert evaluator.evaluate("edit_file", args) is PermissionDecision.DENY
    assert explain_deny(evaluator, "edit_file", args) == evaluator.explain_deny("edit_file", args)
    assert "plan" in explain_deny(evaluator, "edit_file", args)


def test_the_shim_gives_what_the_evaluator_gives_for_a_scoped_deny():
    evaluator = PermissionEvaluator.from_strings(deny=["Bash(rm *)"])
    args = {"command": "rm -rf x"}

    assert explain_deny(evaluator, "Bash", args) == evaluator.explain_deny("Bash", args)
    assert explain_deny(evaluator, "Bash", args) != "Bash is denied by policy"


def test_the_shim_gives_the_generic_line_for_a_call_that_is_not_denied():
    evaluator = PermissionEvaluator.from_strings(mode="bypassPermissions")

    assert explain_deny(evaluator, "edit_file", {}) == "edit_file is denied by policy"


def test_the_shim_is_the_evaluator_not_a_copy(monkeypatch):
    evaluator = PermissionEvaluator.from_strings()
    monkeypatch.setattr(PermissionEvaluator, "explain_deny", lambda self, name, args: "sentinel")

    assert explain_deny(evaluator, "x", {}) == "sentinel"


def test_the_package_imports_cleanly_in_a_fresh_interpreter():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import igni_core.core.config.permission_eval"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_the_bash_command_and_catalog_survive_the_import_chain():
    assert BashCommand("rm x").mutates()
    assert ToolCategoryCatalog.default().is_shell("Bash")
