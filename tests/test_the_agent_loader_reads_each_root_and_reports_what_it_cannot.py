"""``AgentDefinitionLoader``: what a directory yields and what it reports.

A directory gives its ``.md`` files in sorted order; a file that is not an
agent becomes a :class:`LoadError` and the others still load; a plugin's
directory gets its namespace and the security envelope together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.agents.loader import AgentDefinitionLoader
from igni_core.core.agents.plugin_policy import PluginRestrictionPolicy
from igni_core.core.agents.schemas import AgentPriority
from igni_core.core.config.settings import Settings
from igni_core.core.paths import CONFIG_DIR


def _agent(directory: Path, filename: str, name: str, description: str = "d") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody\n", encoding="utf-8")
    return path


def _loader(project: Path, **kwargs) -> AgentDefinitionLoader:
    return AgentDefinitionLoader(Settings(), project, **kwargs)


def test_a_missing_directory_is_an_empty_report(tmp_path) -> None:
    report = _loader(tmp_path).load_directory(tmp_path / "nope", AgentPriority.PROJECT_EMBER)
    assert report.entries == {} and report.errors == []


def test_a_path_that_is_a_file_is_an_empty_report(tmp_path) -> None:
    file = tmp_path / "agents"
    file.write_text("x", encoding="utf-8")
    report = _loader(tmp_path).load_directory(file, AgentPriority.PROJECT_EMBER)
    assert report.entries == {} and report.errors == []


def test_only_md_files_are_agents(tmp_path) -> None:
    directory = tmp_path / "agents"
    _agent(directory, "real.md", "real")
    _agent(directory, "notes.txt", "ignored")
    _agent(directory, "README.md.bak", "ignored-too")
    report = _loader(tmp_path).load_directory(directory, AgentPriority.PROJECT_EMBER)
    assert list(report.entries) == ["real"]
    assert report.errors == []


def test_a_bad_file_is_an_error_with_its_path_and_reason_and_the_rest_load(tmp_path) -> None:
    directory = tmp_path / "agents"
    _agent(directory, "a-good.md", "good")
    bad = directory / "b-bad.md"
    bad.write_text("no frontmatter here", encoding="utf-8")
    nameless = directory / "c-nameless.md"
    nameless.write_text("---\ndescription: d\n---\nbody", encoding="utf-8")
    _agent(directory, "d-also-good.md", "also-good")

    report = _loader(tmp_path).load_directory(directory, AgentPriority.USER_EMBER)

    assert sorted(report.entries) == ["also-good", "good"]
    assert [(e.path, "No YAML frontmatter" in e.reason) for e in report.errors[:1]] == [(bad, True)]
    assert report.errors[1].path == nameless
    assert "missing 'name'" in report.errors[1].reason
    assert len(report.errors) == 2


def test_two_files_declaring_one_name_the_last_sorted_wins(tmp_path) -> None:
    directory = tmp_path / "agents"
    _agent(directory, "a.md", "same", "from a")
    _agent(directory, "b.md", "same", "from b")
    report = _loader(tmp_path).load_directory(directory, AgentPriority.PROJECT_EMBER)
    assert report.entries["same"].definition.description == "from b"


def test_an_integer_priority_becomes_the_enum_and_an_invalid_one_is_refused(tmp_path) -> None:
    directory = tmp_path / "agents"
    _agent(directory, "a.md", "a")
    report = _loader(tmp_path).load_directory(directory, 5)
    assert report.entries["a"].priority is AgentPriority.PROJECT_EMBER
    with pytest.raises(ValueError):
        _loader(tmp_path).load_directory(directory, 99)


def test_a_plugin_roots_priority_that_is_no_agent_priority_is_kept_for_its_namespace(
    tmp_path,
) -> None:
    directory = tmp_path / "agents"
    _agent(directory, "a.md", "a")
    report = _loader(tmp_path).load_directory(directory, 7, namespace="managed")
    assert report.entries["managed:a"].priority == 7
    assert not isinstance(report.entries["managed:a"].priority, AgentPriority)


def test_a_plugin_directory_is_namespaced_and_enveloped_together(tmp_path) -> None:
    directory = tmp_path / "plugin" / "agents"
    directory.mkdir(parents=True)
    (directory / "scout.md").write_text(
        "---\nname: scout\ndescription: d\nmcp_servers: [db]\nhooks: x\n---\nbody", encoding="utf-8"
    )
    loader = _loader(tmp_path, restriction_policy=PluginRestrictionPolicy.strict())
    report = loader.load_directory(directory, AgentPriority.PROJECT_EMBER, namespace="acme")
    definition = report.entries["acme:scout"].definition
    assert definition.name == "acme:scout"
    assert definition.mcp_servers == []
    assert definition.force_isolation == "worktree"


def test_without_a_policy_servers_are_kept(tmp_path) -> None:
    directory = tmp_path / "agents"
    directory.mkdir()
    (directory / "a.md").write_text(
        "---\nname: a\ndescription: d\nmcp_servers: [db]\n---\nbody", encoding="utf-8"
    )
    report = _loader(tmp_path).load_directory(directory, AgentPriority.PROJECT_EMBER)
    definition = report.entries["a"].definition
    assert definition.mcp_servers == ["db"]
    assert definition.force_isolation is None


def test_a_body_anchor_nobody_fills_is_left_for_a_later_pass(tmp_path) -> None:
    directory = tmp_path / "agents"
    directory.mkdir()
    (directory / "a.md").write_text(
        "---\nname: a\ndescription: d\n---\nSee {{AGENT_CATALOG}}.", encoding="utf-8"
    )
    report = _loader(tmp_path).load_directory(directory, AgentPriority.PROJECT_EMBER)
    assert report.entries["a"].definition.system_prompt == "See {{AGENT_CATALOG}}."


def test_load_never_reads_the_ephemeral_directory(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "proj"
    _agent(project / CONFIG_DIR / "agents.tmp", "temp.md", "temp")
    _agent(project / CONFIG_DIR / "agents", "kept.md", "kept")
    assert sorted(_loader(project).load().entries) == ["kept"]


def test_load_reports_the_errors_of_every_root(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "proj"
    broken = project / CONFIG_DIR / "agents" / "broken.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("nothing", encoding="utf-8")
    _agent(home / CONFIG_DIR / "agents", "user.md", "user")
    report = _loader(project).load()
    assert list(report.entries) == ["user"]
    assert [e.path for e in report.errors] == [broken]


def test_the_cross_tool_roots_load_only_when_the_setting_is_on(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "proj"
    _agent(project / ".claude" / "agents", "p.md", "from-project-claude")
    _agent(home / ".claude" / "agents", "u.md", "from-user-claude")

    settings = Settings()
    settings.agents.cross_tool_support = True
    on = AgentDefinitionLoader(settings, project).load()
    assert on.entries["from-project-claude"].priority is AgentPriority.PROJECT_CLAUDE
    assert on.entries["from-user-claude"].priority is AgentPriority.USER_CLAUDE

    settings.agents.cross_tool_support = False
    assert AgentDefinitionLoader(settings, project).load().entries == {}


def test_a_group_directory_is_ranked_between_the_users_and_the_projects(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "proj"
    group = tmp_path / "group"
    _agent(home / CONFIG_DIR / "agents", "u.md", "one", "user")
    _agent(group, "g.md", "one", "group")
    _agent(group, "g2.md", "two", "group")
    _agent(project / CONFIG_DIR / "agents", "p.md", "two", "project")

    entries = _loader(project, group_dir=group).load().entries

    assert entries["one"].definition.description == "group"
    assert entries["one"].priority is AgentPriority.ORG_GROUP
    assert entries["two"].definition.description == "project"


def test_the_prompt_loader_it_is_given_fills_the_anchors_of_every_agent(tmp_path) -> None:
    from igni_core.core.prompts import PromptFragments, PromptLoader

    fragments = PromptFragments()
    fragments.register("SCHEMA", "acme.ext", lambda: "the schema")
    directory = tmp_path / "agents"
    directory.mkdir()
    (directory / "a.md").write_text(
        "---\nname: a\ndescription: d\n---\nUse {{SCHEMA}}.", encoding="utf-8"
    )
    loader = _loader(tmp_path, prompt_loader=PromptLoader(fragments))
    report = loader.load_directory(directory, AgentPriority.PROJECT_EMBER)
    assert report.entries["a"].definition.system_prompt == "Use the schema."
