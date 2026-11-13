"""How the permission store folds its sources into one answer.

Defaults first, then the four settings files in order, then the config's
category levels on top, and last the levels the organisation made final. A
call is answered by the last argument rule that matches it, else by the
tool's own level, else by "ask".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from igni_core.core.config.schemas import PermissionsConfig
from igni_core.core.config.tool_permissions import ToolPermissions
from igni_core.core.paths import project_config_dir


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _write(path: Path, permissions: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": permissions}))


def _project_file(project: Path, name: str = "settings.json") -> Path:
    return project_config_dir(project) / name


def _home_file(name: str = "settings.json") -> Path:
    return Path.home() / ".igni" / name


def _final(cfg: PermissionsConfig, *fields: str) -> PermissionsConfig:
    cfg.set_final_fields(frozenset(fields))
    return cfg


# ── the files, in order ───────────────────────────────────────────


def test_with_no_files_and_no_config_every_tool_has_its_default(project):
    perms = ToolPermissions(project_dir=project)

    for tool in ("Write", "Edit", "Bash", "BashOutput", "WebSearch", "WebFetch", "NotebookEdit"):
        assert perms.get_level(tool) == "ask"
    assert not perms.has_arg_rules("Bash")


def test_a_later_file_overrides_an_earlier_one_for_the_same_tool(project):
    _write(_home_file("settings.json"), {"deny": ["Bash"], "allow": ["Edit"]})
    _write(_home_file("settings.local.json"), {"ask": ["Bash"]})
    _write(_project_file(project), {"allow": ["Bash"], "deny": ["Edit"]})

    perms = ToolPermissions(project_dir=project)

    assert perms.get_level("Bash") == "allow"
    assert perms.get_level("Edit") == "deny"
    assert perms.is_denied("Edit")

    _write(_project_file(project, "settings.local.json"), {"deny": ["Bash"]})
    assert ToolPermissions(project_dir=project).get_level("Bash") == "deny"


def test_a_rule_that_is_not_a_rule_is_skipped_with_a_warning(project, caplog):
    _write(_project_file(project), {"allow": ["Two Words", "Edit"], "deny": ["Bash()"]})

    with caplog.at_level(logging.WARNING):
        perms = ToolPermissions(project_dir=project)

    assert perms.get_level("Edit") == "allow"
    assert perms.get_level("Bash") == "ask"
    assert "Skipping unparseable permission rule: 'Two Words'" in caplog.text
    assert "'Bash()'" in caplog.text


def test_a_corrupt_file_does_not_stop_construction(project):
    _project_file(project).parent.mkdir(parents=True)
    _project_file(project).write_text("{nope")

    assert ToolPermissions(project_dir=project).get_level("Bash") == "ask"


def test_without_a_project_directory_the_working_directory_is_the_project(project, monkeypatch):
    _write(_project_file(project), {"allow": ["Bash"]})
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: project))

    assert ToolPermissions().get_level("Bash") == "allow"


# ── answering a call ──────────────────────────────────────────────


def test_the_last_matching_argument_rule_wins_across_files_even_when_it_is_the_looser(project):
    _write(_project_file(project), {"deny": ["Bash(git *)"]})
    _write(
        _project_file(project, "settings.local.json"),
        {"allow": ["Bash(git status)"], "ask": ["Bash(git push *)"]},
    )
    perms = ToolPermissions(project_dir=project)

    assert perms.check("Bash", tool_args={"command": "git status"}) == "allow"
    assert perms.check("Bash", tool_args={"command": "git push origin"}) == "ask"
    assert perms.check("Bash", tool_args={"command": "git log"}) == "deny"
    assert perms.check("Bash", tool_args={"command": "ls"}) == "ask"


def test_inside_one_file_a_deny_is_read_after_an_ask_and_an_allow(project):
    """A file lists allow, then ask, then deny, so where one file's own rules
    overlap the deny is the last to match and wins."""
    _write(_project_file(project), {"deny": ["Bash(git *)"], "allow": ["Bash(git status)"]})
    perms = ToolPermissions(project_dir=project)

    assert perms.check("Bash", tool_args={"command": "git status"}) == "deny"


def test_an_argument_rule_is_found_through_the_function_name_the_model_called(project):
    _write(_project_file(project), {"allow": ["Bash(git status)"], "deny": ["Edit(src/*)"]})
    perms = ToolPermissions(project_dir=project)

    assert (
        perms.check("", func_name="run_shell_command", tool_args={"command": "git status"})
        == "allow"
    )
    assert perms.check("", func_name="edit_file", tool_args={"path": "src/a.py"}) == "deny"
    assert perms.check("", func_name="edit_file", tool_args={"path": "docs/a.py"}) == "ask"


def test_a_tool_nobody_has_a_rule_for_is_asked_about_whatever_its_arguments(project):
    perms = ToolPermissions(project_dir=project)

    assert perms.check("SomeNewTool") == "ask"
    assert perms.check("SomeNewTool", tool_args={"command": "ls"}) == "ask"
    assert perms.check("") == "ask"


def test_a_call_with_no_arguments_matches_no_argument_rule_but_a_catch_all(project):
    _write(_project_file(project), {"allow": ["Bash(git status)"], "deny": ["Edit(*)"]})
    perms = ToolPermissions(project_dir=project)

    assert perms.check("Bash", tool_args=None) == "ask"
    assert perms.check("Bash", tool_args={}) == "ask"
    assert perms.check("Edit", tool_args={}) == "deny"


def test_the_bare_level_answers_when_no_argument_rule_matches(project):
    _write(_project_file(project), {"deny": ["WebFetch"], "allow": ["WebFetch(domain:github.com)"]})
    perms = ToolPermissions(project_dir=project)

    assert perms.check("WebFetch", tool_args={"url": "https://github.com/a"}) == "allow"
    assert perms.check("WebFetch", tool_args={"url": "https://evil.test/a"}) == "deny"
    assert perms.has_arg_rules("WebFetch")
    assert not perms.has_arg_rules("Bash")


def test_a_confirmation_is_needed_only_at_ask_and_never_in_bypass_mode(project):
    _write(_project_file(project), {"allow": ["Edit"], "deny": ["Write"]})
    perms = ToolPermissions(project_dir=project)

    assert perms.needs_confirmation("Bash") is True
    assert perms.needs_confirmation("Edit") is False
    assert perms.needs_confirmation("Write") is False
    assert perms.needs_confirmation("Unknown") is True

    bypass = ToolPermissions(
        project_dir=project, settings_permissions=PermissionsConfig(mode="bypassPermissions")
    )
    assert bypass.needs_confirmation("Bash") is False
    # The mode silences the pause; it does not rewrite the level.
    assert bypass.get_level("Bash") == "ask"
    assert bypass.get_level("Write") == "deny"

    plan = ToolPermissions(project_dir=project, settings_permissions=PermissionsConfig(mode="plan"))
    assert plan.needs_confirmation("Bash") is True


# ── the config's category levels ──────────────────────────────────


def test_a_config_level_that_differs_from_the_default_overrides_the_files(project):
    _write(_project_file(project), {"deny": ["Write"], "allow": ["WebFetch", "WebSearch"]})
    cfg = PermissionsConfig(file_write="allow", web_fetch="deny", shell_execute="allow")

    perms = ToolPermissions(project_dir=project, settings_permissions=cfg)

    assert perms.get_level("Write") == "allow"
    assert perms.get_level("Edit") == "allow"
    assert perms.get_level("Bash") == "allow"
    assert perms.get_level("BashOutput") == "allow"
    assert perms.get_level("WebFetch") == "deny"
    # web_search stayed at its default, so the file's allow is not overwritten.
    assert perms.get_level("WebSearch") == "allow"


def test_a_config_level_equal_to_the_default_leaves_the_files_alone(project):
    _write(_project_file(project), {"allow": ["Bash", "Write"]})

    perms = ToolPermissions(
        project_dir=project, settings_permissions=PermissionsConfig(shell_execute="ask")
    )

    assert perms.get_level("Bash") == "allow"
    assert perms.get_level("Write") == "allow"


def test_a_level_that_is_not_allow_ask_or_deny_is_skipped_with_a_warning(project, caplog):
    cfg = PermissionsConfig.model_construct(
        file_read="ask", file_write="bogus", shell_execute="ask", web_search="ask", web_fetch="ask"
    )

    with caplog.at_level(logging.WARNING):
        perms = ToolPermissions(project_dir=project, settings_permissions=cfg)

    assert perms.get_level("Write") == "ask"
    assert "PermissionsConfig has invalid level 'bogus' for tools ['Write', 'Edit']" in caplog.text


# ── the levels the organisation made final ────────────────────────


def test_a_final_level_replaces_the_files_and_drops_a_looser_argument_rule(project, caplog):
    _write(
        _project_file(project),
        {
            "allow": ["Bash", "Bash(git status)", "Edit(src/*)"],
            "ask": ["Bash(make *)"],
            "deny": ["Bash(rm *)"],
        },
    )
    cfg = _final(PermissionsConfig(shell_execute="ask"), "shell_execute")

    with caplog.at_level(logging.INFO):
        perms = ToolPermissions(project_dir=project, settings_permissions=cfg)

    assert perms.get_level("Bash") == "ask"
    # Looser than the organisation's ask: gone. Equal or stricter: kept.
    assert perms.check("Bash", tool_args={"command": "git status"}) == "ask"
    assert perms.check("Bash", tool_args={"command": "make all"}) == "ask"
    assert perms.check("Bash", tool_args={"command": "rm x"}) == "deny"
    assert perms.has_arg_rules("Bash")
    # Another category is not the organisation's to touch.
    assert perms.check("Edit", tool_args={"path": "src/a.py"}) == "allow"
    assert "Dropped 1 permission rule(s) for Bash, BashOutput, Python" in caplog.text
    assert "shell_execute: ask" in caplog.text


def test_a_final_deny_leaves_no_argument_rule_that_lets_a_call_through(project):
    _write(
        _project_file(project),
        {"allow": ["WebFetch(domain:github.com)"], "ask": ["WebFetch(domain:a.test)"]},
    )
    cfg = _final(PermissionsConfig(web_fetch="deny"), "web_fetch")

    perms = ToolPermissions(project_dir=project, settings_permissions=cfg)

    assert perms.get_level("WebFetch") == "deny"
    assert not perms.has_arg_rules("WebFetch")
    assert perms.check("WebFetch", tool_args={"url": "https://github.com/x"}) == "deny"


def test_a_final_allow_keeps_every_argument_rule(project):
    _write(_project_file(project), {"deny": ["Write(secrets/*)"], "ask": ["Edit(docs/*)"]})
    cfg = _final(PermissionsConfig(file_write="allow"), "file_write")

    perms = ToolPermissions(project_dir=project, settings_permissions=cfg)

    assert perms.get_level("Write") == "allow"
    assert perms.get_level("Edit") == "allow"
    assert perms.check("Write", tool_args={"path": "secrets/k"}) == "deny"
    assert perms.check("Edit", tool_args={"path": "docs/a"}) == "ask"


def test_a_final_field_whose_level_is_not_a_level_is_ignored(project):
    _write(_project_file(project), {"allow": ["Write", "Write(a/*)"]})
    cfg = PermissionsConfig.model_construct(file_write="bogus")
    cfg.set_final_fields(frozenset({"file_write"}))

    perms = ToolPermissions(project_dir=project, settings_permissions=cfg)

    assert perms.get_level("Write") == "allow"
    assert perms.has_arg_rules("Write")


def test_a_field_that_is_not_final_does_not_get_the_treatment(project):
    _write(_project_file(project), {"allow": ["Bash", "Bash(ls)"]})
    cfg = _final(PermissionsConfig(shell_execute="ask"), "file_write")

    perms = ToolPermissions(project_dir=project, settings_permissions=cfg)

    assert perms.get_level("Bash") == "allow"
    assert perms.check("Bash", tool_args={"command": "ls"}) == "allow"


# ── saving a rule ─────────────────────────────────────────────────


def test_a_saved_bare_rule_changes_the_level_at_once_and_on_disk(project):
    perms = ToolPermissions(project_dir=project)

    perms.save_rule("Bash", "allow")

    assert perms.get_level("Bash") == "allow"
    assert perms.needs_confirmation("Bash") is False
    saved = json.loads(_project_file(project, "settings.local.json").read_text())
    assert saved == {"permissions": {"allow": ["Bash"]}}
    assert ToolPermissions(project_dir=project).get_level("Bash") == "allow"


def test_a_saved_argument_rule_is_seen_by_the_next_check_and_by_a_new_store(project):
    perms = ToolPermissions(project_dir=project)

    perms.save_rule("Bash(git push)", "allow")

    assert perms.check("Bash", tool_args={"command": "git push"}) == "allow"
    assert perms.check("Bash", tool_args={"command": "git pull"}) == "ask"
    assert (
        ToolPermissions(project_dir=project).check("Bash", tool_args={"command": "git push"})
        == "allow"
    )


def test_moving_a_saved_rule_to_another_level_is_what_the_next_check_says(project):
    perms = ToolPermissions(project_dir=project)

    perms.save_rule("Bash(git push)", "allow")
    perms.save_rule("Bash(git push)", "deny")
    assert perms.check("Bash", tool_args={"command": "git push"}) == "deny"

    perms.save_rule("Bash(git push)", "allow")
    assert perms.check("Bash", tool_args={"command": "git push"}) == "allow"
    assert (
        ToolPermissions(project_dir=project).check("Bash", tool_args={"command": "git push"})
        == "allow"
    )


def test_a_save_that_cannot_be_written_leaves_the_memory_as_it_was(project, monkeypatch):
    perms = ToolPermissions(project_dir=project)
    blocker = _project_file(project, "settings.local.json")
    blocker.parent.mkdir(parents=True)
    blocker.mkdir()

    with pytest.raises(OSError):
        perms.save_rule("Bash", "allow")

    assert perms.get_level("Bash") == "ask"
