"""``HitlPermissions`` answers a permission check and reports what a sticky choice saved.

``test_hitl_always_persists`` pins the files a choice writes and the
evaluator it patches. These pin the answers: the level a check gives,
the catalog name winning over the caller's, and the result of a choice
that saves a rule, one that does not, and one that cannot.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.hitl_permissions import HitlPermissions
from igni_core.backend.schemas_hitl import ToolCallArgs
from igni_core.core.config.permission_eval import PermissionDecision, PermissionEvaluator
from igni_core.core.paths import CONFIG_DIR
from igni_core.protocol import messages as msg


def _permissions(project: Path, evaluator: PermissionEvaluator | None = None) -> HitlPermissions:
    session = SimpleNamespace(project_dir=project, permission_evaluator=evaluator)
    return HitlPermissions(session)  # type: ignore[arg-type]


def _requirement(tool_name: str | None, **args: Any) -> Any:
    execution = SimpleNamespace(tool_name=tool_name, tool_args=args) if tool_name else None
    return SimpleNamespace(tool_execution=execution)


def _saved(project: Path) -> dict[str, list[str]]:
    path = project / CONFIG_DIR / "settings.local.json"
    return json.loads(path.read_text())["permissions"] if path.exists() else {}


# ── check ─────────────────────────────────────────────────────────


def test_a_saved_deny_rule_is_what_a_check_answers(tmp_path: Path):
    permissions = _permissions(tmp_path)
    permissions.save_rule("Bash(rm -rf build)", "deny")
    permissions.save_rule("Bash(make)", "allow")

    denied = permissions.check("Bash", "run_shell_command", ToolCallArgs(command="rm -rf build"))
    allowed = permissions.check("Bash", "run_shell_command", ToolCallArgs(command="make"))
    unmatched = permissions.check("Bash", "run_shell_command", ToolCallArgs(command="ls"))

    assert (denied, allowed, unmatched) == ("deny", "allow", "ask")


def test_the_catalog_name_of_the_function_wins_over_the_name_the_caller_gave(tmp_path: Path):
    permissions = _permissions(tmp_path)
    permissions.save_rule("Bash(make)", "deny")

    wrongly_named = permissions.check("Read", "run_shell_command", ToolCallArgs(command="make"))

    assert wrongly_named == "deny"


def test_a_function_the_catalog_does_not_know_is_checked_under_the_callers_name(tmp_path: Path):
    permissions = _permissions(tmp_path)
    permissions.save_rule("acme_tool", "allow")

    assert permissions.check("acme_tool", "acme_tool", ToolCallArgs()) == "allow"
    assert permissions.check("other_tool", "other_tool", ToolCallArgs()) == "ask"


# ── persist_choice ────────────────────────────────────────────────


def test_always_reports_the_exact_rule_it_saved_and_its_level(tmp_path: Path):
    result = _permissions(tmp_path).persist_choice(
        msg.HITLDecision(choice="always"), _requirement("run_shell_command", command="make all")
    )

    assert (result.ok, result.rule, result.level) == (True, "Bash(make all)", "allow")
    assert _saved(tmp_path) == {"allow": ["Bash(make all)"]}


def test_similar_reports_the_pattern_rule_and_deny_reports_a_deny_level(tmp_path: Path):
    permissions = _permissions(tmp_path)

    similar = permissions.persist_choice(
        msg.HITLDecision(choice="similar"), _requirement("run_shell_command", command="make all")
    )
    deny = permissions.persist_choice(
        msg.HITLDecision(choice="deny"), _requirement("run_shell_command", command="rm -rf /")
    )

    assert (similar.ok, similar.rule, similar.level) == (True, "Bash(make *)", "allow")
    assert (deny.ok, deny.rule, deny.level) == (True, "Bash(rm -rf /)", "deny")
    assert _saved(tmp_path) == {"allow": ["Bash(make *)"], "deny": ["Bash(rm -rf /)"]}


def test_the_choice_is_read_without_regard_to_case(tmp_path: Path):
    result = _permissions(tmp_path).persist_choice(
        msg.HITLDecision.model_construct(choice="ALWAYS"),
        _requirement("run_shell_command", command="ls"),
    )

    assert result.ok and result.rule == "Bash(ls)"


@pytest.mark.parametrize("choice", ["once", "", "banana"])
def test_a_choice_that_is_not_sticky_saves_nothing_and_says_why(tmp_path: Path, choice: str):
    result = _permissions(tmp_path).persist_choice(
        msg.HITLDecision.model_construct(choice=choice),
        _requirement("run_shell_command", command="ls"),
    )

    assert not result.ok
    assert result.reason == f"choice '{choice}' is not persistable"
    assert _saved(tmp_path) == {}


def test_a_requirement_that_names_no_tool_saves_nothing_says_why_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.WARNING, logger="igni_core.backend.hitl_permissions")

    result = _permissions(tmp_path).persist_choice(
        msg.HITLDecision(choice="always"), _requirement(None)
    )

    assert not result.ok
    assert result.reason == "missing tool metadata on requirement"
    assert _saved(tmp_path) == {}
    assert any("missing tool metadata" in r.getMessage() for r in caplog.records)


def test_the_rule_is_written_under_the_catalog_name_whichever_name_the_requirement_carried(
    tmp_path: Path,
):
    permissions = _permissions(tmp_path)

    from_function = permissions.persist_choice(
        msg.HITLDecision(choice="always"), _requirement("run_shell_command", command="ls")
    )
    from_catalog = permissions.persist_choice(
        msg.HITLDecision(choice="always"), _requirement("Bash", command="ls")
    )

    assert from_function.rule == from_catalog.rule == "Bash(ls)"


# ── what the running session holds ────────────────────────────────


def test_a_rule_that_does_not_parse_is_left_out_of_the_evaluator(tmp_path: Path):
    evaluator = PermissionEvaluator()
    permissions = _permissions(tmp_path, evaluator)

    permissions.save_rule("   ", "deny")

    assert evaluator.deny == [] and evaluator.allow == [] and evaluator.ask == []


def test_a_choice_holds_the_running_session_to_it_at_once(tmp_path: Path):
    evaluator = PermissionEvaluator()
    permissions = _permissions(tmp_path, evaluator)

    permissions.persist_choice(
        msg.HITLDecision(choice="deny"), _requirement("run_shell_command", command="rm -rf /")
    )

    assert evaluator.evaluate("Bash", {"command": "rm -rf /"}) is PermissionDecision.DENY
