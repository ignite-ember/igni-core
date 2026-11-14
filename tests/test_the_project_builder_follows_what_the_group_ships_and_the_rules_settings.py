"""The project builder: the group's layout, the scaffold, the project's instructions.

The first run scaffolds the project's own files, except what the person's
group ships itself: bundled agents, the built-in hook registration and the
hook scripts stand down, so an admin's removal is not scaffolded back and
two sources do not fight over one file. The project's instructions read
``CLAUDE.md`` only while ``rules.cross_tool_support`` is on.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.session.core import Session
from igni_core.core.session.parts.project import ProjectBuilder
from tests.session_factory import stub_project, stub_settings

BUNDLED_AGENT = Path(".igni") / "agents" / "reviewer.md"
BUNDLED_SETTINGS = Path(".igni") / "settings.json"
BUNDLED_HOOK = Path(".igni") / "hooks" / "pre-pr-review.sh"


def _session(root: Path, *, group_ships: str | None = None, edit=None) -> tuple[Session, Path]:
    project = stub_project(root / "proj")
    settings = stub_settings(project, home=root / "home")
    if edit is not None:
        edit(settings)
    if group_ships is not None:
        shipped = GroupPolicyLayout.for_data_dir(settings.storage.data_dir).ensure_dir(group_ships)
        (shipped / "from-the-group.md").write_text("---\nname: from-the-group\n---\n\nx\n")
    return Session(settings=settings, project_dir=project), project


def test_the_group_layout_is_rooted_in_the_storage_data_dir(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)

    expected = GroupPolicyLayout.for_data_dir(session.settings.storage.data_dir)
    assert session.group_policy.root == expected.root


def test_a_session_with_no_group_pack_scaffolds_the_bundled_files(tmp_path: Path) -> None:
    _, project = _session(tmp_path)

    assert (project / BUNDLED_AGENT).is_file()
    assert (project / BUNDLED_SETTINGS).is_file()
    assert (project / BUNDLED_HOOK).is_file()


def test_bundled_agents_are_not_scaffolded_back_when_the_group_ships_agents(
    tmp_path: Path,
) -> None:
    _, project = _session(tmp_path, group_ships="agents")

    assert not (project / BUNDLED_AGENT).exists()
    assert (project / BUNDLED_HOOK).is_file()


def test_builtin_hooks_are_not_registered_when_the_group_ships_hooks(tmp_path: Path) -> None:
    _, project = _session(tmp_path, group_ships="hooks")

    assert not (project / BUNDLED_SETTINGS).exists()
    assert (project / BUNDLED_HOOK).is_file()


def test_no_hook_scripts_are_written_when_the_group_ships_scripts(tmp_path: Path) -> None:
    _, project = _session(tmp_path, group_ships="scripts")

    assert not (project / BUNDLED_HOOK).exists()
    assert (project / BUNDLED_AGENT).is_file()


def test_running_the_initializer_again_keeps_what_the_first_run_wrote(tmp_path: Path) -> None:
    session, project = _session(tmp_path)
    agent = project / BUNDLED_AGENT
    agent.write_text("my own reviewer\n")

    ProjectBuilder(session).run_initializer()

    assert agent.read_text() == "my own reviewer\n"


def test_claude_md_is_read_into_the_instructions_while_cross_tool_support_is_on(
    tmp_path: Path,
) -> None:
    def on(settings) -> None:
        settings.rules.cross_tool_support = True

    session, project = _session(tmp_path, edit=on)
    (project / "CLAUDE.md").write_text("Use tabs, never spaces.\n")

    ProjectBuilder(session).build_context()

    assert "Use tabs, never spaces." in session.project_instructions


def test_claude_md_is_ignored_when_cross_tool_support_is_off(tmp_path: Path) -> None:
    def off(settings) -> None:
        settings.rules.cross_tool_support = False

    session, project = _session(tmp_path, edit=off)
    (project / "CLAUDE.md").write_text("Use tabs, never spaces.\n")

    ProjectBuilder(session).build_context()

    assert "Use tabs, never spaces." not in session.project_instructions


def test_the_rules_index_is_given_the_group_s_rules_only_when_it_ships_some(
    tmp_path: Path,
) -> None:
    without, _ = _session(tmp_path / "a")
    shipped, _ = _session(tmp_path / "b", group_ships="rules")

    assert without.rules_index._group_rules_dir is None
    assert shipped.rules_index._group_rules_dir == shipped.group_policy.dir_for("rules")


def test_the_rules_index_follows_the_cross_tool_setting(tmp_path: Path) -> None:
    def off(settings) -> None:
        settings.rules.cross_tool_support = False

    session, _ = _session(tmp_path, edit=off)

    assert session.rules_index._read_claude_md is False
