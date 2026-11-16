"""``--strict`` denies what has no allow rule, and an allow rule opts a tool in.

``--strict`` used to set the file-write and shell categories to ``deny``. The
registry drops a tool whose bare level is ``deny`` before the agent is built,
and the evaluator runs deny first, so ``Bash``, ``Write`` and ``Edit`` never
reached the agent and no allow rule could bring them back (campaign R010,
R105). ``dontAsk`` is what denies the unmatched call; the flag only has to
take back the allows a looser flag set.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.cli.options import CliOptions
from igni_core.core.config.cli_overrides import CliOverrides
from igni_core.core.config.permission_eval import PermissionDecision, PermissionEvaluator
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.config.tool_permissions.store import ToolPermissions
from igni_core.core.paths import CONFIG_DIR


def _strict_settings(allow: str, **flags: bool):
    config = Path.home() / CONFIG_DIR / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"permissions:\n  allow: {allow}\n")
    overrides = CliOverrides.from_options(CliOptions(strict=True, **flags))
    return SettingsLoader.load(cli_overrides=overrides)


def test_an_allowed_tool_reaches_the_agent_under_strict(tmp_path: Path) -> None:
    settings = _strict_settings('["Bash(cat *)", "Write"]')
    store = ToolPermissions(project_dir=tmp_path, settings_permissions=settings.permissions)
    for tool in ("Bash", "Write", "Edit"):
        assert not store.is_denied(tool), tool


def test_strict_allows_only_what_a_rule_allows() -> None:
    settings = _strict_settings('["Bash(cat *)", "Write"]')
    ev = PermissionEvaluator.from_permissions(settings.permissions)
    shell = "run_shell_command"
    assert ev.evaluate(shell, {"command": "cat a.txt"}) is PermissionDecision.ALLOW
    assert ev.evaluate(shell, {"command": "ls"}) is PermissionDecision.DENY
    assert ev.evaluate("file_read", {"file_path": "a.txt"}) is PermissionDecision.DENY
    assert ev.evaluate("git_push", {}) is PermissionDecision.DENY


def test_strict_takes_back_what_auto_approve_allowed() -> None:
    settings = _strict_settings("[]", auto_approve=True, accept_edits=True)
    ev = PermissionEvaluator.from_permissions(settings.permissions)
    assert ev.evaluate("run_shell_command", {"command": "ls"}) is PermissionDecision.DENY
    assert ev.evaluate("save_file", {"file_name": "x", "contents": "y"}) is (
        PermissionDecision.DENY
    )


def test_strict_keeps_read_only_refusing_writes() -> None:
    settings = _strict_settings('["Write"]', read_only=True)
    assert settings.permissions.file_write == "deny"
