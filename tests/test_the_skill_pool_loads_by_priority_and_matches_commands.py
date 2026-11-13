"""The skill pool: the org's group row, ties, bad files, grouping, commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.paths import CONFIG_DIR
from igni_core.core.skills.loader import SkillPool, SkillPriority


def _skill(root: Path, dirname: str, text: str | None = None, **fm) -> None:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    if text is None:
        lines = [f"{k}: {v}" for k, v in {"name": dirname, **fm}.items()]
        text = "---\n" + "\n".join(lines) + "\n---\nbody\n"
    (d / "SKILL.md").write_text(text)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    home, project, group = tmp_path / "home", tmp_path / "project", tmp_path / "group"
    for p in (home, project, group):
        p.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    return home, project, group


def _load(layout, **kw):
    _, project, group = layout
    pool = SkillPool()
    pool.load_all(project_dir=project, group_dir=group, **kw)
    return pool


def test_the_priority_constants_are_fixed_integers():
    assert [
        SkillPriority.BUNDLED,
        SkillPriority.USER_CLAUDE,
        SkillPriority.USER_EMBER,
        SkillPriority.ORG_GROUP,
        SkillPriority.PROJECT_CLAUDE,
        SkillPriority.PROJECT_LOCAL,
        SkillPriority.PROJECT_EMBER,
    ] == [0, 1, 2, 3, 4, 5, 6]


def test_the_group_skill_beats_the_users_global_skill(layout):
    home, _, group = layout
    _skill(home / CONFIG_DIR / "skills", "ship", description="user")
    _skill(group, "ship", description="group")
    assert _load(layout).get("ship").description == "group"


def test_the_group_skill_beats_the_users_claude_skill(layout):
    home, _, group = layout
    _skill(home / ".claude" / "skills", "ship", description="user-claude")
    _skill(group, "ship", description="group")
    assert _load(layout, cross_tool_support=True).get("ship").description == "group"


def test_a_project_skill_beats_the_group_skill(layout):
    _, project, group = layout
    _skill(group, "ship", description="group")
    _skill(project / CONFIG_DIR / "skills", "ship", description="project")
    assert _load(layout).get("ship").description == "project"


def test_a_project_claude_skill_beats_the_group_skill(layout):
    _, project, group = layout
    _skill(group, "ship", description="group")
    _skill(project / ".claude" / "skills", "ship", description="project-claude")
    assert _load(layout, cross_tool_support=True).get("ship").description == "project-claude"


def test_load_all_with_no_skills_anywhere_loads_nothing_quietly(layout, capsys):
    assert _load(layout).list_skills() == []
    assert capsys.readouterr().err == ""


def test_without_a_group_dir_no_group_skill_loads(layout):
    _, project, group = layout
    _skill(group, "ship", description="group")
    pool = SkillPool()
    pool.load_all(project_dir=project)
    assert pool.get("ship") is None


def test_project_dir_defaults_to_the_current_directory(layout, monkeypatch):
    _, project, _ = layout
    _skill(project / CONFIG_DIR / "skills", "here", description="cwd")
    monkeypatch.setattr("pathlib.Path.cwd", lambda: project)
    pool = SkillPool()
    pool.load_all()
    assert pool.get("here").description == "cwd"


def test_the_config_directory_name_is_taken_from_config_dir(layout):
    _, project, _ = layout
    _skill(project / CONFIG_DIR / "skills.local", "mine", description="local")
    assert CONFIG_DIR == ".igni"
    assert _load(layout).get("mine").description == "local"


def test_equal_priority_keeps_the_first_loaded(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _skill(a, "x", description="first")
    _skill(b, "x", description="second")
    pool = SkillPool()
    pool.load_directory(a, priority=3)
    pool.load_directory(b, priority=3)
    assert pool.get("x").description == "first"


def test_a_lower_priority_never_replaces_a_higher_one(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _skill(a, "x", description="high")
    _skill(b, "x", description="low")
    pool = SkillPool()
    pool.load_directory(a, priority=5)
    pool.load_directory(b, priority=1)
    assert pool.get("x").description == "high"


def test_a_namespace_prefixes_the_name_and_keeps_plugins_apart(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _skill(a, "x", description="from a")
    _skill(b, "x", description="from b")
    pool = SkillPool()
    pool.load_directory(a, namespace="pa")
    pool.load_directory(b, namespace="pb")
    assert pool.get("x") is None
    assert pool.get("pa:x").description == "from a"
    assert pool.get("pb:x").description == "from b"


def test_a_missing_directory_is_a_silent_no_op(tmp_path, capsys):
    pool = SkillPool()
    pool.load_directory(tmp_path / "nope")
    assert pool.list_skills() == []
    assert capsys.readouterr().err == ""


def test_an_unparseable_skill_warns_on_stderr_and_the_rest_still_load(tmp_path, capsys):
    _skill(tmp_path, "good", description="ok")
    _skill(tmp_path, "bad", text="---\nname: [unclosed\n---\nbody\n")
    pool = SkillPool()
    pool.load_directory(tmp_path)
    err = capsys.readouterr().err
    assert pool.get("good") is not None
    assert pool.get("bad") is None
    assert err.startswith("Warning: Failed to load skill from ")
    assert str(tmp_path / "bad" / "SKILL.md") in err


def test_skills_load_sorted_by_name(tmp_path):
    for n in ("b", "c", "a"):
        _skill(tmp_path, n)
    pool = SkillPool()
    pool.load_directory(tmp_path)
    assert [s.name for s in pool.list_skills()] == ["a", "b", "c"]


def test_list_by_category_groups_skills(tmp_path):
    _skill(tmp_path, "a", category="review")
    _skill(tmp_path, "b", category="review")
    _skill(tmp_path, "c")
    pool = SkillPool()
    pool.load_directory(tmp_path)
    grouped = pool.list_by_category()
    assert {k: [s.name for s in v] for k, v in grouped.items()} == {
        "review": ["a", "b"],
        "development": ["c"],
    }


def test_an_empty_pool_describes_as_an_empty_string():
    assert SkillPool().describe() == ""


def test_describe_orders_known_categories_then_the_rest_alphabetically(tmp_path):
    _skill(tmp_path, "z-ops", category="operations", description="d4")
    _skill(tmp_path, "y-plan", category="planning", description="d3")
    _skill(tmp_path, "x-rev", category="review", description="d2")
    _skill(tmp_path, "w-dev", category="development", description="d1")
    _skill(tmp_path, "v-zeta", category="zeta", description="d6")
    _skill(tmp_path, "u-alpha", category="alpha", description="d5")
    pool = SkillPool()
    pool.load_directory(tmp_path)
    assert pool.describe() == (
        "\n### Development\n- **/w-dev**: d1"
        "\n\n### Review\n- **/x-rev**: d2"
        "\n\n### Planning\n- **/y-plan**: d3"
        "\n\n### Operations\n- **/z-ops**: d4"
        "\n\n### Alpha\n- **/u-alpha**: d5"
        "\n\n### Zeta\n- **/v-zeta**: d6"
    )


def test_describe_sorts_skills_by_name_and_shows_the_argument_hint(tmp_path):
    _skill(tmp_path, "b", description="second", **{"argument-hint": "'<file>'"})
    _skill(tmp_path, "a", description="first")
    pool = SkillPool()
    pool.load_directory(tmp_path)
    assert pool.describe() == "\n### Development\n- **/a**: first\n- **/b** <file>: second"


@pytest.fixture
def pool(tmp_path):
    _skill(tmp_path, "review", description="r")
    _skill(tmp_path, "hidden", **{"user-invocable": "false"})
    _skill(tmp_path, "ünï", description="u")
    p = SkillPool()
    p.load_directory(tmp_path)
    return p


def test_a_command_with_arguments_matches_and_keeps_the_rest_of_the_line(pool):
    skill, args = pool.match_user_command("  /review   src/a.py   --fast  ")
    assert skill.name == "review"
    assert args == "src/a.py   --fast"


def test_a_command_without_arguments_has_empty_arguments(pool):
    skill, args = pool.match_user_command("/review")
    assert (skill.name, args) == ("review", "")


def test_a_unicode_skill_name_matches(pool):
    assert pool.match_user_command("/ünï x")[0].name == "ünï"


@pytest.mark.parametrize("text", ["/", "/ ", "  /  ", "review", "", "/nope", "/hidden"])
def test_text_that_names_no_invocable_skill_matches_nothing(pool, text):
    assert pool.match_user_command(text) is None
